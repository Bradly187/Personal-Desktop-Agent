import Combine
import Foundation

// MARK: - A2UICanvasStore
//
// Holds the single persistent dashboard canvas shown on the Agent tab. Lives at
// app scope (injected as an EnvironmentObject) so it survives tab switches —
// AgentDashboardView is created/destroyed as the user moves between tabs, so the
// canvas state must NOT live inside the view.
//
// Persistence: the last canvas is cached to UserDefaults and restored on launch,
// so the dashboard renders instantly on a cold start, then refreshes when the PC
// reconnects and pushes an update.
//
// Feeds (from WebSocketManager):
//   a2uiCanvasFeed        → set/replace the whole canvas
//   a2uiCanvasUpdateFeed  → merge components by id (partial refresh)
//   a2uiCanvasClearFeed   → reset to empty

@MainActor
final class A2UICanvasStore: ObservableObject {
    @Published private(set) var canvas: A2UISurface?

    private let cacheKey = "a2ui.canvas.last"
    private var cancellables = Set<AnyCancellable>()

    init() {
        canvas = loadCached()
    }

    /// Subscribe to the WebSocket canvas feeds. Called once after injection.
    func bind(to ws: WebSocketManager) {
        ws.a2uiCanvasFeed
            .receive(on: DispatchQueue.main)
            .sink { [weak self] surface in self?.set(surface) }
            .store(in: &cancellables)

        ws.a2uiCanvasUpdateFeed
            .receive(on: DispatchQueue.main)
            .sink { [weak self] nodes in self?.merge(nodes) }
            .store(in: &cancellables)

        ws.a2uiCanvasClearFeed
            .receive(on: DispatchQueue.main)
            .sink { [weak self] in self?.clear() }
            .store(in: &cancellables)
    }

    private func set(_ surface: A2UISurface) {
        canvas = surface
        persist(surface)
    }

    private func merge(_ nodes: [A2UINode]) {
        guard let current = canvas else {
            AppLogger.shared.warning("a2ui", "canvas update with no base canvas — ignored")
            return
        }
        let merged = current.merging(nodes)
        canvas = merged
        persist(merged)
    }

    private func clear() {
        canvas = nil
        UserDefaults.standard.removeObject(forKey: cacheKey)
    }

    // MARK: - Cache

    private func persist(_ surface: A2UISurface) {
        if let data = try? JSONEncoder().encode(surface) {
            UserDefaults.standard.set(data, forKey: cacheKey)
        }
    }

    private func loadCached() -> A2UISurface? {
        guard let data = UserDefaults.standard.data(forKey: cacheKey) else { return nil }
        return try? JSONDecoder().decode(A2UISurface.self, from: data)
    }
}
