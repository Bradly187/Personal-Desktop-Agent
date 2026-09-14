import Combine
import Foundation

/// Syncs feature toggle changes from SettingsStore to the PC via WebSocket.
///
/// Observes the feature toggle `@Published` properties and sends
/// `set_feature_toggle` messages whenever one changes. If the WebSocket is
/// disconnected, changes are queued locally and flushed on reconnection.
///
/// **Intentionally subscription-free as of 2026-08-16 — do not delete.**
/// Its only subscriber was `edgeScrollEnabled`, which drove the `edge_scroll`
/// feature deleted with the gaze removal; the toggle was repurposed as the
/// purely iPad-side `momentumScrollEnabled` (R7.3). The send / queue / flush
/// machinery below is kept deliberately, mirroring the PC's own
/// `FusionEngine.VALID_FEATURES = set()` — which its authors left wired "without
/// special-casing" so the next real toggle needs no rebuild. Removing this class
/// would mean re-implementing offline queueing from scratch.
///
/// Requirements: 4.3, 4.7; `specs/ipad-trackpad-ergonomics/` R7.4
@MainActor
final class FeatureToggleSyncer {

    // MARK: — Dependencies

    private let settings: SettingsStore
    private let ws: WebSocketManager
    private var cancellables = Set<AnyCancellable>()

    // MARK: — Pending queue (offline support)

    /// Queued toggle changes awaiting WebSocket reconnection.
    /// Key is the wire feature name, value is the desired enabled state.
    /// Later changes for the same feature overwrite earlier ones (last-write-wins).
    private var pendingToggles: [String: Bool] = [:]

    // MARK: — Init

    init(settings: SettingsStore, ws: WebSocketManager) {
        self.settings = settings
        self.ws = ws
        _subscribeToToggles()
        _subscribeToConnectionState()
    }

    // MARK: — Toggle observation

    private func _subscribeToToggles() {
        // No feature toggles are currently synced to the PC. Add a subscription
        // here when one exists, calling `_handleToggleChange(feature:enabled:)`
        // with the wire name. `momentumScrollEnabled` deliberately does NOT
        // belong here — momentum is entirely iPad-side (R7.3).
    }

    // MARK: — Connection state observation

    private func _subscribeToConnectionState() {
        ws.$state
            .removeDuplicates()
            .sink { [weak self] state in
                guard let self else { return }
                if state == .connected {
                    self._flushPendingToggles()
                }
            }
            .store(in: &cancellables)
    }

    // MARK: — Send or queue

    private func _handleToggleChange(feature: String, enabled: Bool) {
        if ws.state == .connected {
            _sendToggle(feature: feature, enabled: enabled)
        } else {
            // Queue for later — last-write-wins per feature
            pendingToggles[feature] = enabled
        }
    }

    private func _sendToggle(feature: String, enabled: Bool) {
        ws.send([
            "type": "set_feature_toggle",
            "feature": feature,
            "enabled": enabled,
        ])
    }

    private func _flushPendingToggles() {
        guard !pendingToggles.isEmpty else { return }
        let toFlush = pendingToggles
        pendingToggles.removeAll()
        for (feature, enabled) in toFlush {
            _sendToggle(feature: feature, enabled: enabled)
        }
    }
}
