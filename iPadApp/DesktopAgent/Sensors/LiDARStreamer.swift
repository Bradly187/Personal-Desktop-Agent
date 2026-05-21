import ARKit
import CoreImage
import Foundation
import UIKit

/// Streams LiDAR depth frames and rear camera images to the PC via WebSocket.
///
/// Sends two message types:
///   depth_frame  — smoothed float32 depth map + uint8 confidence map, base64-encoded,
///                  matching the format LiDARReceiver.on_depth_frame() expects on the PC.
///   camera_frame — JPEG rear camera image downscaled to 480 px wide, base64-encoded,
///                  matching the format GestureProcessor.on_camera_frame() expects.
///
/// Also publishes UIImages of both feeds so LiDARDebugView can display them locally
/// without waiting for the PC to echo anything back.
///
/// Throttle defaults: depth at 5 fps, camera at 10 fps — tunable via init params.
///
/// NOTE: Uses ARWorldTrackingConfiguration (rear LiDAR + camera). Running this
/// alongside GazeTracker / HeadTracker (front TrueDepth ARFaceTrackingConfiguration)
/// is supported on iPad Pro 2020+ but increases thermal load. Both sessions use
/// different physical cameras, so they do not compete for the same sensor.
@MainActor
final class LiDARStreamer: NSObject, ObservableObject {

    // MARK: - Published state (LiDARDebugView binds here)

    @Published private(set) var latestCameraImage: UIImage?
    @Published private(set) var latestDepthImage: UIImage?
    @Published private(set) var depthFps: Double = 0
    @Published private(set) var cameraFps: Double = 0
    @Published private(set) var validPixelPct: Double = 0
    @Published private(set) var depthRangeMin: Double = 0
    @Published private(set) var depthRangeMax: Double = 0
    @Published private(set) var isRunning = false

    // MARK: - Static capability check

    static var isSupported: Bool {
        ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }

    // MARK: - Private

    private weak var ws: WebSocketManager?
    private let session = ARSession()
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])
    private let bgQueue = DispatchQueue(label: "lidar.streamer.bg", qos: .userInitiated)

    private let depthInterval: Double   // seconds between depth sends
    private let cameraInterval: Double  // seconds between camera sends

    // Throttle state accessed only from the ARSessionDelegate serial callback.
    private let _t = _LiDARThrottle()

    // Auto-recovery state
    private var recoveryTask: Task<Void, Never>?
    private var recoveryAttempts = 0
    private let maxRecoveryAttempts = 3

    // MARK: - Init

    init(ws: WebSocketManager, depthFps: Double = 5, cameraFps: Double = 10) {
        self.ws = ws
        self.depthInterval = 1.0 / depthFps
        self.cameraInterval = 1.0 / cameraFps
        super.init()
        session.delegate = self
    }

    // MARK: - Lifecycle

    func start() {
        guard !isRunning else { return }
        guard LiDARStreamer.isSupported else {
            AppLogger.shared.warning("LiDARStreamer", "sceneDepth not supported — requires iPad Pro 2020+ with LiDAR")
            return
        }
        let config = ARWorldTrackingConfiguration()
        config.frameSemantics = .smoothedSceneDepth
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        isRunning = true
        recoveryAttempts = 0
        _t.fpsWindowStart = CACurrentMediaTime()
        AppLogger.shared.info("LiDARStreamer", "Started — depth \(Int(1/depthInterval)) fps, camera \(Int(1/cameraInterval)) fps")
    }

    func stop() {
        guard isRunning else { return }
        recoveryTask?.cancel()
        recoveryTask = nil
        session.pause()
        isRunning = false
        // Fix #22: Clear stale published state so UI doesn't show old frames after stop
        latestCameraImage = nil
        latestDepthImage = nil
        depthFps = 0
        cameraFps = 0
        validPixelPct = 0
        depthRangeMin = 0
        depthRangeMax = 0
        AppLogger.shared.info("LiDARStreamer", "Stopped")
    }
}

// MARK: - ARSessionDelegate

extension LiDARStreamer: ARSessionDelegate {

    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let now = CACurrentMediaTime()

        // Throttle depth
        if let smoothed = frame.smoothedSceneDepth, now - _t.lastDepthTime >= depthInterval {
            _t.lastDepthTime = now
            _t.depthCountWindow += 1
            bgQueue.async { [weak self] in self?.processDepthFrame(smoothed, ts: now) }
        }

        // Throttle camera
        if now - _t.lastCameraTime >= cameraInterval {
            _t.lastCameraTime = now
            _t.cameraCountWindow += 1
            let pixelBuffer = frame.capturedImage
            bgQueue.async { [weak self] in self?.processCameraFrame(pixelBuffer, ts: now) }
        }

        // Update fps counters once per second
        if _t.fpsWindowStart == 0 { _t.fpsWindowStart = now }
        let elapsed = now - _t.fpsWindowStart
        if elapsed >= 1.0 {
            let df = Double(_t.depthCountWindow) / elapsed
            let cf = Double(_t.cameraCountWindow) / elapsed
            _t.depthCountWindow = 0
            _t.cameraCountWindow = 0
            _t.fpsWindowStart = now
            Task { @MainActor [weak self] in
                self?.depthFps = df
                self?.cameraFps = cf
            }
        }
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        AppLogger.shared.error("LiDARStreamer", "ARSession error — \(error.localizedDescription)")
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.isRunning = false
            self._attemptRecovery()
        }
    }
}

// MARK: - Auto-recovery

private extension LiDARStreamer {
    func _attemptRecovery() {
        guard recoveryAttempts < maxRecoveryAttempts else {
            AppLogger.shared.error("LiDARStreamer", "Max recovery attempts (\(maxRecoveryAttempts)) reached — giving up")
            return
        }

        recoveryAttempts += 1
        let attempt = recoveryAttempts
        AppLogger.shared.warning("LiDARStreamer", "Attempting recovery (\(attempt)/\(maxRecoveryAttempts)) in 2s")

        recoveryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000) // 2 second delay
            guard !Task.isCancelled else { return }
            guard let self else { return }
            self.start()
        }
    }
}

// MARK: - Frame processing (runs on bgQueue)

private extension LiDARStreamer {

    // MARK: Depth

    func processDepthFrame(_ depthData: ARDepthData, ts: Double) {
        let depthMap = depthData.depthMap
        guard CVPixelBufferGetPixelFormatType(depthMap) == kCVPixelFormatType_DepthFloat32 else { return }

        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        let w = CVPixelBufferGetWidth(depthMap)
        let h = CVPixelBufferGetHeight(depthMap)
        let stride = CVPixelBufferGetBytesPerRow(depthMap)
        guard let base = CVPixelBufferGetBaseAddress(depthMap) else {
            CVPixelBufferUnlockBaseAddress(depthMap, .readOnly)
            return
        }

        // Build packed depth bytes (strip row padding) + RGBA debug image in one pass
        var depthBytes = Data(capacity: w * h * 4)
        var rgbaPixels = [UInt8](repeating: 255, count: w * h * 4)
        var validCount = 0
        var minD: Float = .infinity, maxD: Float = -.infinity

        for row in 0..<h {
            let rowPtr = base.advanced(by: row * stride).bindMemory(to: Float32.self, capacity: w)
            // Packed send bytes: w floats, no row-stride padding
            depthBytes.append(Data(bytes: rowPtr, count: w * MemoryLayout<Float32>.size))
            for col in 0..<w {
                let d = rowPtr[col]
                let i = (row * w + col) * 4
                if d.isFinite && d > 0 {
                    validCount += 1
                    if d < minD { minD = d }
                    if d > maxD { maxD = d }
                    // Hue 240 (blue) = near, Hue 0 (red) = far
                    let t = min(d / 4.0, 1.0)
                    let (r, g, b) = _hueToRGB(hue: (1.0 - t) * 240.0)
                    rgbaPixels[i] = r; rgbaPixels[i+1] = g; rgbaPixels[i+2] = b
                } else {
                    rgbaPixels[i] = 40; rgbaPixels[i+1] = 40; rgbaPixels[i+2] = 40
                }
                rgbaPixels[i+3] = 255
            }
        }
        CVPixelBufferUnlockBaseAddress(depthMap, .readOnly)

        // Confidence map
        var confBytes = Data(capacity: w * h)
        var validPct = 0.0
        if let confMap = depthData.confidenceMap {
            CVPixelBufferLockBaseAddress(confMap, .readOnly)
            let cw = CVPixelBufferGetWidth(confMap)
            let ch = CVPixelBufferGetHeight(confMap)
            let cStride = CVPixelBufferGetBytesPerRow(confMap)
            if let cBase = CVPixelBufferGetBaseAddress(confMap) {
                for row in 0..<ch {
                    confBytes.append(cBase.advanced(by: row * cStride)
                        .assumingMemoryBound(to: UInt8.self), count: cw)
                }
            }
            CVPixelBufferUnlockBaseAddress(confMap, .readOnly)
            validPct = Double(validCount) / Double(w * h) * 100.0
        }

        // Build debug UIImage
        let depthImage: UIImage? = rgbaPixels.withUnsafeMutableBytes { buf in
            guard let ctx = CGContext(
                data: buf.baseAddress, width: w, height: h,
                bitsPerComponent: 8, bytesPerRow: w * 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ), let cg = ctx.makeImage() else { return nil }
            return UIImage(cgImage: cg)
        }

        let depthB64 = depthBytes.base64EncodedString()
        let confB64  = confBytes.isEmpty ? "" : confBytes.base64EncodedString()
        let finalMin = minD == .infinity  ? 0.0 : Double(minD)
        let finalMax = maxD == -.infinity ? 0.0 : Double(maxD)

        // Send WebSocket message directly from bgQueue — WebSocketManager.send() is thread-safe
        // (dispatches to its own serial sendQueue). No MainActor hop needed for the send.
        ws?.sendDepthFrame(width: w, height: h,
                          depthB64: depthB64, confB64: confB64,
                          ts: ts * 1000)

        // Only hop to MainActor for UI state updates
        Task { @MainActor [weak self] in
            guard let self, self.isRunning else { return }
            if let img = depthImage { latestDepthImage = img }
            validPixelPct = validPct
            depthRangeMin = finalMin
            depthRangeMax = finalMax
        }
    }

    // MARK: Camera

    func processCameraFrame(_ pixelBuffer: CVPixelBuffer, ts: Double) {
        // Downscale to max 480 px wide, encode as JPEG
        let origW = CGFloat(CVPixelBufferGetWidth(pixelBuffer))
        let origH = CGFloat(CVPixelBufferGetHeight(pixelBuffer))
        let scale  = min(1.0, 480.0 / origW)
        let targetW = Int(origW * scale)
        let targetH = Int(origH * scale)

        let ci = CIImage(cvPixelBuffer: pixelBuffer)
            .transformed(by: CGAffineTransform(scaleX: scale, y: scale))

        guard let cg = ciContext.createCGImage(ci, from: CGRect(x: 0, y: 0,
                                                                 width: targetW,
                                                                 height: targetH))
        else { return }

        let uiImg = UIImage(cgImage: cg)
        guard let jpeg = uiImg.jpegData(compressionQuality: 0.7) else { return }
        let imageB64 = jpeg.base64EncodedString()

        // Send WebSocket message directly from bgQueue — thread-safe via sendQueue
        ws?.sendCameraFrame(width: targetW, height: targetH,
                           imageB64: imageB64, ts: ts * 1000)

        // Only hop to MainActor for UI state
        Task { @MainActor [weak self] in
            guard let self, self.isRunning else { return }
            latestCameraImage = uiImg
        }
    }
}

// MARK: - Throttle state (Swift 5.9-compatible alternative to nonisolated(unsafe))

/// Mutable throttle counters accessed only from ARSessionDelegate's serial callback.
/// @unchecked Sendable: safe because ARSessionDelegate never fires concurrently.
private final class _LiDARThrottle: @unchecked Sendable {
    var lastDepthTime: Double = 0
    var lastCameraTime: Double = 0
    var depthCountWindow: Int = 0
    var cameraCountWindow: Int = 0
    var fpsWindowStart: Double = 0
}

// MARK: - Colour helpers

/// Maps a fully-saturated HSV colour (S=1, V=1) at `hue` (0–360°) to RGB bytes.
private func _hueToRGB(hue: Float) -> (UInt8, UInt8, UInt8) {
    let h = hue / 60.0
    let i = Int(h) % 6
    let f = h - floor(h)
    switch i {
    case 0: return (255,            UInt8(f * 255),     0)
    case 1: return (UInt8((1-f)*255), 255,              0)
    case 2: return (0,              255,                UInt8(f * 255))
    case 3: return (0,              UInt8((1-f)*255),   255)
    case 4: return (UInt8(f * 255), 0,                  255)
    default: return (255,           0,                  UInt8((1-f)*255))
    }
}
