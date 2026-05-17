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
///
/// Fix #7: Consumer handlers are called directly on the ARKit delegate thread
/// (no DispatchQueue.main.async hop) to eliminate ~16ms latency. Consumers that
/// need main-thread access must dispatch internally.
///
/// Fix #4: On ARSession error, attempts automatic recovery after 1 second.
/// Notifies consumers via onError callback so they can update UI state.
@MainActor
final class SharedFaceSession: NSObject {

    // MARK: - Public

    /// Whether ARFaceTracking is supported on this device.
    static var isSupported: Bool {
        ARFaceTrackingConfiguration.isSupported
    }

    /// Whether the session is currently running.
    @Published private(set) var isRunning = false

    /// Fix #4: Error callback for consumers to observe session failures.
    var onError: ((Error) -> Void)?

    // MARK: - Private

    private let session = ARSession()

    /// Lock protecting _unsafeConsumers for thread-safe access from ARKit delegate thread.
    /// Fix #7: Consumers are called from the delegate thread, so we need thread-safe access.
    private let consumersLock = NSLock()

    /// Backing storage for consumers — accessed under consumersLock from any thread.
    /// nonisolated(unsafe) opts out of actor isolation so the nonisolated delegate can read it.
    nonisolated(unsafe) private var _unsafeConsumers: [String: @Sendable (ARFaceAnchor) -> Void] = [:]

    /// Fix #4: Recovery state
    private var recoveryTask: Task<Void, Never>?
    private var recoveryAttempts = 0
    private let maxRecoveryAttempts = 3

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
    ///   - handler: Called on the ARKit delegate thread with each updated ARFaceAnchor.
    ///             Must be Sendable. Dispatch to main internally if needed.
    func addConsumer(_ id: String, handler: @escaping @Sendable (ARFaceAnchor) -> Void) {
        consumersLock.lock()
        _unsafeConsumers[id] = handler
        consumersLock.unlock()

        if !isRunning {
            _start()
        }
    }

    /// Removes a consumer. Pauses the session when the last consumer is removed.
    func removeConsumer(_ id: String) {
        consumersLock.lock()
        _unsafeConsumers.removeValue(forKey: id)
        let isEmpty = _unsafeConsumers.isEmpty
        consumersLock.unlock()

        if isEmpty {
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
        recoveryAttempts = 0
    }

    private func _stop() {
        recoveryTask?.cancel()
        recoveryTask = nil
        session.pause()
        isRunning = false
    }

    // MARK: - Fix #4: Recovery

    private func _attemptRecovery() {
        guard recoveryAttempts < maxRecoveryAttempts else {
            print("SharedFaceSession: max recovery attempts reached, giving up")
            return
        }

        consumersLock.lock()
        let hasConsumers = !_unsafeConsumers.isEmpty
        consumersLock.unlock()

        guard hasConsumers else { return }

        recoveryAttempts += 1
        let attempt = recoveryAttempts
        print("SharedFaceSession: attempting recovery (\(attempt)/\(maxRecoveryAttempts))")

        recoveryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_000_000_000) // 1 second delay
            guard !Task.isCancelled else { return }
            guard let self else { return }
            await MainActor.run {
                self._start()
            }
        }
    }
}

// MARK: - ARSessionDelegate

extension SharedFaceSession: ARSessionDelegate {
    /// Fix #7: Deliver face anchor updates directly on the ARKit delegate thread.
    /// No DispatchQueue.main.async — eliminates ~16ms latency per frame.
    nonisolated func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        guard let face = anchors.first(where: { $0 is ARFaceAnchor }) as? ARFaceAnchor else {
            return
        }
        // Thread-safe read of consumers
        consumersLock.lock()
        let handlers = Array(_unsafeConsumers.values)
        consumersLock.unlock()

        for handler in handlers {
            handler(face)
        }
    }

    /// Fix #4: On error, notify consumers and attempt automatic recovery.
    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        print("SharedFaceSession: ARSession error — \(error.localizedDescription)")
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.isRunning = false
            self.onError?(error)
            self._attemptRecovery()
        }
    }
}
