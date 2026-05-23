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
///
/// Fix: Signal processing runs on a dedicated serial queue (not MainActor) to eliminate
/// the ~8-16ms latency from Task { @MainActor } hops. WebSocketManager.send() already
/// dispatches to its own serial sendQueue, so calling it from processQueue is safe.
@MainActor
final class GazeTracker: NSObject, ObservableObject {

    private let sharedFaceSession: SharedFaceSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "GazeTracker"

    /// Serial processing queue — all gaze math runs here, off the ARKit delegate thread
    /// and off MainActor. Eliminates the Task { @MainActor } hop latency (~8-16ms).
    private let processQueue = DispatchQueue(label: "gaze.tracker.process", qos: .userInteractive)

    // All mutable processing state is accessed ONLY from processQueue.
    // nonisolated(unsafe) opts out of actor isolation for these fields.
    nonisolated(unsafe) private var prevGazeDir: simd_float3?
    nonisolated(unsafe) private var filterDx: OneEuroFilter
    nonisolated(unsafe) private var filterDy: OneEuroFilter
    nonisolated(unsafe) private var saccadeState: SaccadeState = .tracking
    nonisolated(unsafe) private var saccadeExitStart: CFTimeInterval?
    nonisolated(unsafe) private var consecutiveNilFrames: Int = 0

    /// Most recent world-space gaze ray in ARKit device-fixed coordinates.
    /// Updated every frame alongside delta computation. All reads and writes
    /// happen on `processQueue` (a single serial dispatch queue), so no atomicity
    /// is required — the 24-byte tuple write would NOT be atomic if accessed
    /// off-queue, so do not read this field from MainActor or other threads.
    /// Used by whoever fires sendGazeDwell to attach calibration ray data.
    nonisolated(unsafe) var currentWorldRay: (origin: simd_float3, dir: simd_float3)?

    // Rate-limit gaze_ray WebSocket messages to ~10 Hz (every 6th frame at 60 Hz)
    nonisolated(unsafe) private var rayFrameCounter: Int = 0
    private static let rayFrameInterval: Int = 6

    // Saccade detection state machine
    private enum SaccadeState {
        case tracking       // Normal output
        case saccade        // Output suppressed (velocity > enter threshold)
        case rampIn(start: CFTimeInterval)  // Ramping from 0 to full over 50ms
    }
    private static let saccadeRampDuration: Double = 0.05  // 50ms ramp-in after saccade

    // Blink/tracking-loss detection
    private static let maxNilBeforeReset: Int = 3  // Reset filter after 3 missed frames

    /// Snapshotted settings values — captured at start() to avoid cross-isolation reads at 60Hz.
    /// Updated via updateSettings() when user changes them.
    nonisolated(unsafe) private var gazeEnabled: Bool = true
    nonisolated(unsafe) private var gazeSensitivity: Double = 200.0
    nonisolated(unsafe) private var saccadeEnterThreshold: Double = 100.0
    nonisolated(unsafe) private var saccadeExitThreshold: Double = 50.0

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
            AppLogger.shared.warning("GazeTracker", "ARFaceTracking not supported on this device")
            return
        }
        guard let settings, settings.gazeEnabled else { return }

        // Snapshot settings for use on processQueue (avoids cross-isolation reads)
        gazeEnabled = settings.gazeEnabled
        gazeSensitivity = settings.gazeSensitivity
        saccadeEnterThreshold = settings.gazeSaccadeEnterThreshold
        saccadeExitThreshold = settings.gazeSaccadeExitThreshold

        prevGazeDir = nil
        filterDx.reset()
        filterDy.reset()
        saccadeState = .tracking
        saccadeExitStart = nil
        consecutiveNilFrames = 0

        sharedFaceSession.addConsumer(Self.consumerID) { [weak self] anchor in
            guard let self else { return }
            // Dispatch to dedicated serial queue — no MainActor hop, eliminates ~8-16ms latency
            self.processQueue.async { [weak self] in
                self?.handleAnchorUpdate(anchor)
            }
        }
    }

    func stop() {
        sharedFaceSession.removeConsumer(Self.consumerID)
        processQueue.async { [weak self] in
            self?.prevGazeDir = nil
            self?.filterDx.reset()
            self?.filterDy.reset()
            self?.saccadeState = .tracking
            self?.saccadeExitStart = nil
            self?.consecutiveNilFrames = 0
        }
    }

    /// Update snapshotted settings when user changes them. Call from MainActor.
    func updateSettings() {
        guard let settings else { return }
        let enabled = settings.gazeEnabled
        let sensitivity = settings.gazeSensitivity
        let enter = settings.gazeSaccadeEnterThreshold
        let exit = settings.gazeSaccadeExitThreshold
        processQueue.async { [weak self] in
            self?.gazeEnabled = enabled
            self?.gazeSensitivity = sensitivity
            self?.saccadeEnterThreshold = enter
            self?.saccadeExitThreshold = exit
        }
    }

    // MARK: — Gaze delta extraction (runs on processQueue)

    /// Processes a face anchor update. Runs entirely on processQueue — no MainActor involvement.
    private func handleAnchorUpdate(_ anchor: ARFaceAnchor) {
        guard gazeEnabled else { return }

        let gazeDir = extractGazeDirection(from: anchor)

        // World-space ray: faceAnchor.transform places the face in ARKit device-fixed
        // coordinates (the iPad is stationary on the desk, so this IS world-fixed).
        // Eye transforms are relative to the face anchor — premultiply to get world space.
        let leftEyeWorld  = anchor.transform * anchor.leftEyeTransform
        let rightEyeWorld = anchor.transform * anchor.rightEyeTransform
        let leftPos  = simd_float3(leftEyeWorld.columns.3.x,  leftEyeWorld.columns.3.y,  leftEyeWorld.columns.3.z)
        let rightPos = simd_float3(rightEyeWorld.columns.3.x, rightEyeWorld.columns.3.y, rightEyeWorld.columns.3.z)
        let eyeMidpoint = (leftPos + rightPos) / 2
        // ARKit eye transforms: forward direction = -Z column
        let leftFwd  = simd_float3(-leftEyeWorld.columns.2.x,  -leftEyeWorld.columns.2.y,  -leftEyeWorld.columns.2.z)
        let rightFwd = simd_float3(-rightEyeWorld.columns.2.x, -rightEyeWorld.columns.2.y, -rightEyeWorld.columns.2.z)
        let worldGazeDir = simd_normalize((leftFwd + rightFwd) / 2)
        currentWorldRay = (origin: eyeMidpoint, dir: worldGazeDir)

        // Blink/tracking-loss detection
        guard let gazeDir else {
            consecutiveNilFrames += 1
            if consecutiveNilFrames >= Self.maxNilBeforeReset {
                filterDx.reset()
                filterDy.reset()
                prevGazeDir = nil
            }
            return
        }
        consecutiveNilFrames = 0

        guard let prev = prevGazeDir else {
            prevGazeDir = gazeDir
            return
        }

        // Compute angular delta in the gaze direction
        let rawDx = gazeDir.x - prev.x
        let rawDy = gazeDir.y - prev.y

        prevGazeDir = gazeDir

        // Axis correction: ARKit eye forward vector Y decreases when looking down,
        // but we need positive dy for downward gaze (cursor moves down).
        let correctedDx = rawDx
        let correctedDy = -rawDy

        // If calibration is active, send raw deltas to handler (dispatch to MainActor for UI)
        if calibrationHandler != nil {
            Task { @MainActor [weak self] in
                self?.calibrationHandler?(rawDx, rawDy)
            }
            return
        }

        let now = CACurrentMediaTime()

        // 1-Euro adaptive filter (per-axis)
        let filteredDx = filterDx.filter(Double(correctedDx), timestamp: now)
        let filteredDy = filterDy.filter(Double(correctedDy), timestamp: now)

        // Compute gaze velocity (degrees/second) for saccade detection
        let frameRate: Double = 60.0
        let velocityDegPerSec = sqrt(filteredDx * filteredDx + filteredDy * filteredDy)
            * (180.0 / .pi) * frameRate

        // Saccade detection state machine (uses snapshotted thresholds)
        let saccadeEnter = saccadeEnterThreshold
        let saccadeExit = saccadeExitThreshold
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
                    saccadeState = .rampIn(start: now)
                    saccadeExitStart = nil
                }
            } else {
                saccadeExitStart = nil
            }

        case .rampIn(let rampStart):
            let elapsed = now - rampStart
            if elapsed >= Self.saccadeRampDuration {
                saccadeState = .tracking
                outputScale = 1.0
            } else {
                outputScale = elapsed / Self.saccadeRampDuration
            }
            if velocityDegPerSec > saccadeEnter {
                saccadeState = .saccade
                saccadeExitStart = nil
                outputScale = 0.0
                isSaccade = true
            }
        }

        // Confidence weighting via eye openness proxy
        let blinkLeft = Float(truncating: anchor.blendShapes[.eyeBlinkLeft] ?? 0)
        let blinkRight = Float(truncating: anchor.blendShapes[.eyeBlinkRight] ?? 0)
        let avgBlink = Double((blinkLeft + blinkRight) / 2.0)
        let confidence = max(0.0, min(1.0, 1.0 - avgBlink * 1.5))

        // Apply output scale (saccade suppression) and confidence weighting
        let scaledDx = filteredDx * outputScale * confidence
        let scaledDy = filteredDy * outputScale * confidence

        // Scale to useful cursor movement range (uses snapshotted sensitivity)
        let sensitivity = gazeSensitivity
        let dx = scaledDx * sensitivity
        let dy = scaledDy * sensitivity

        // Send world-space gaze ray at ~10 Hz (every 6th frame) for monitor calibration.
        // MUST run BEFORE the delta-zero guard below — during dwell the user holds
        // their gaze still, dx/dy collapse to ~0, and the guard returns. If we sent
        // gaze_ray after the guard, the PC's `_latest_gaze_ray_ts` would go stale
        // exactly when GazeCalibrator.project() needs a fresh ray for the dwell click.
        rayFrameCounter += 1
        if rayFrameCounter >= Self.rayFrameInterval {
            rayFrameCounter = 0
            if let ray = currentWorldRay {
                ws?.sendGazeRay(
                    dx: Double(ray.dir.x),
                    dy: Double(ray.dir.y),
                    dz: Double(ray.dir.z),
                    confidence: confidence
                )
            }
        }

        // Only suppress truly zero output to avoid unnecessary WebSocket traffic.
        guard abs(dx) > 0.01 || abs(dy) > 0.01 else { return }

        // WebSocketManager.send() dispatches to its own serial sendQueue — safe from here
        ws?.sendGazeDelta(dx: dx, dy: dy, confidence: confidence, saccade: isSaccade)
    }

    /// Extract the average gaze direction vector from both eyes.
    /// Returns nil if eyes are closed or data is unreliable.
    private func extractGazeDirection(from anchor: ARFaceAnchor) -> simd_float3? {
        let blinkLeft = Float(truncating: anchor.blendShapes[.eyeBlinkLeft] ?? 0)
        let blinkRight = Float(truncating: anchor.blendShapes[.eyeBlinkRight] ?? 0)
        guard blinkLeft < 0.7 && blinkRight < 0.7 else { return nil }

        let leftEye = anchor.leftEyeTransform
        let rightEye = anchor.rightEyeTransform

        let leftFwd = simd_float3(-leftEye.columns.2.x, -leftEye.columns.2.y, -leftEye.columns.2.z)
        let rightFwd = simd_float3(-rightEye.columns.2.x, -rightEye.columns.2.y, -rightEye.columns.2.z)

        return normalize((leftFwd + rightFwd) / 2)
    }
}
