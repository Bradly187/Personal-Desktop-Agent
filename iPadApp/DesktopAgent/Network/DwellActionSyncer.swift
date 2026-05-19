import Combine
import Foundation

/// Syncs dwell action type changes from SettingsStore to the PC via WebSocket.
///
/// Observes `activeDwellAction` and sends `set_dwell_action` messages whenever
/// it changes. If the WebSocket is disconnected, the latest action type is queued
/// locally and sent on reconnection (last-write-wins — only the most recent
/// selection matters).
///
/// Requirements: 6.3, 6.7
@MainActor
final class DwellActionSyncer {

    // MARK: — Dependencies

    private let settings: SettingsStore
    private let ws: WebSocketManager
    private var cancellables = Set<AnyCancellable>()

    // MARK: — Pending queue (offline support)

    /// The queued dwell action type awaiting WebSocket reconnection.
    /// Only the latest selection is kept (last-write-wins).
    private var pendingAction: DwellActionType?

    // MARK: — Init

    init(settings: SettingsStore, ws: WebSocketManager) {
        self.settings = settings
        self.ws = ws
        _subscribeToActionChanges()
        _subscribeToConnectionState()
    }

    // MARK: — Action observation

    private func _subscribeToActionChanges() {
        settings.$activeDwellAction
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] action in
                self?._handleActionChange(action)
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
                    self._flushPendingAction()
                }
            }
            .store(in: &cancellables)
    }

    // MARK: — Send or queue

    private func _handleActionChange(_ action: DwellActionType) {
        if ws.state == .connected {
            ws.sendSetDwellAction(action)
        } else {
            // Queue for later — last-write-wins
            pendingAction = action
        }
    }

    private func _flushPendingAction() {
        guard let action = pendingAction else { return }
        pendingAction = nil
        ws.sendSetDwellAction(action)
    }
}
