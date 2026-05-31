import Combine
import Foundation

/// Syncs feature toggle changes from SettingsStore to the PC via WebSocket.
///
/// Observes the feature toggle `@Published` properties and sends
/// `set_feature_toggle` messages whenever one changes. If the WebSocket is
/// disconnected, changes are queued locally and flushed on reconnection.
///
/// Requirements: 4.3, 4.7
@MainActor
final class FeatureToggleSyncer {

    // MARK: — Wire feature name mapping

    /// Maps Swift property key paths to the wire protocol feature names.
    private static let featureNameMap: [String: String] = [
        "edgeScrollEnabled": "edge_scroll",
    ]

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
        settings.$edgeScrollEnabled
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] enabled in
                self?._handleToggleChange(feature: "edge_scroll", enabled: enabled)
            }
            .store(in: &cancellables)
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
