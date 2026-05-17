import ARKit
import Foundation

/// Shared ARFaceTrackingConfiguration session for GazeTracker and HeadTracker.
///
/// ARKit only supports one face-tracking session at a time per device. Previously,
/// GazeTracker and HeadTracker each created their own ARSession — whichever started
/// second would steal the camera from the first, causing silent failures.
///
/// This class owns a single ARSession and fans out face anchor updates to all
/// registered consumers. Both sensors register a handler and receive the same
/// ARFaceAnchor data from one session.
///
/// Reference-counted: the session runs while at least one consumer is registered,
/// and pauses when the last consumer is removed.
@MainActor
final class SharedFaceSession: NSObject {

    // MARK: - Public

    /// Whether ARFaceTracking is supported on this device.
    static var isSupported: Bool {
        ARFaceTrackingConfiguration.isSupported
    }

    /// Whether the session is currently running.
    private(set) var isRunning = false

    // MARK: - Private

    private let session = ARSession()
    private var consumers: [String: (ARFaceAnchor) -> Void] = [:]

    override init() {
        super.init()
        session.delegate = self
    }

    // MARK: - Consumer Management

    /// Registers a consumer to receive face anchor updates.
    /// Starts the session if this is the first consumer.
    ///
    /// - Parameters:
    ///   - id: Unique identifier for the consumer (e.g., "GazeTracker", "HeadTracker")
    ///   - handler: Called on the main thread with each updated ARFaceAnchor.
    func addConsumer(_ id: String, handler: @escaping (ARFaceAnchor) -> Void) {
        consumers[id] = handler
        if !isRunning {
            _start()
        }
    }

    /// Removes a consumer. Pauses the session when the last consumer is removed.
    func removeConsumer(_ id: String) {
        consumers.removeValue(forKey: id)
        if consumers.isEmpty {
            _stop()
        }
    }

    // MARK: - Session Lifecycle

    private func _start() {
        guard Self.isSupported else {
            print("SharedFaceSession: ARFaceTracking not supported on this device")
            return
        }
        let config = ARFaceTrackingConfiguration()
        config.isLightEstimationEnabled = false
        session.run(config)
        isRunning = true
    }

    private func _stop() {
        session.pause()
        isRunning = false
    }
}

// MARK: - ARSessionDelegate

extension SharedFaceSession: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        guard let face = anchors.first(where: { $0 is ARFaceAnchor }) as? ARFaceAnchor else {
            return
        }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            for handler in self.consumers.values {
                handler(face)
            }
        }
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        print("SharedFaceSession: ARSession error — \(error.localizedDescription)")
        DispatchQueue.main.async { [weak self] in
            self?.isRunning = false
        }
    }
}
