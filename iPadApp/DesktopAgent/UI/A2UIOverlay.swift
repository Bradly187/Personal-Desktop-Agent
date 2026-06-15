import SwiftUI

// MARK: - A2UIOverlay
//
// Floating overlay that renders the current agent-pushed A2UI surface and sends
// the user's tap back to the PC as an `a2ui_event`. Follows the same overlay
// discipline as ProactiveNotificationOverlay / DwellToolbarContainer: only the
// card itself takes hit-testing, so the rest of the screen stays interactive.
//
// One surface at a time (v1): a new `a2ui_surface` replaces the live one; an
// `a2ui_clear` for the matching surface_id dismisses it. A stale clear for a
// superseded surface is ignored.

struct A2UIOverlay: ViewModifier {
    @EnvironmentObject var wsManager: WebSocketManager

    @State private var current: A2UISurface?
    @State private var state: A2UIState?
    @State private var timeoutTask: Task<Void, Never>?

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .center) {
                if let surface = current, let state = state {
                    A2UIRenderer(surface: surface, state: state)
                        .frame(maxWidth: 480)
                        .padding(DesignTokens.Spacing.lg)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.lg))
                        .shadow(color: .black.opacity(0.2), radius: 12, y: 4)
                        .transition(.scale.combined(with: .opacity))
                        .allowsHitTesting(true)
                }
            }
            .onReceive(wsManager.a2uiFeed) { surface in present(surface) }
            .onReceive(wsManager.a2uiClearFeed) { surfaceId in
                if current?.surfaceId == surfaceId { dismiss() }
            }
    }

    private func present(_ surface: A2UISurface) {
        let st = A2UIState { event, value, values in
            wsManager.sendA2UIEvent(surfaceId: surface.surfaceId,
                                    event: event, value: value, values: values)
            // A tap resolves the surface; the PC decides what to show next.
            dismiss()
        }
        withAnimation(.spring(response: 0.3, dampingFraction: 0.85)) {
            current = surface
            state = st
        }
        // Client-side timeout: fire an explicit `timeout` event so the PC's
        // fail-safe (e.g. approval → DENY) triggers deterministically.
        timeoutTask?.cancel()
        let timeout = surface.timeoutS
        timeoutTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
            guard !Task.isCancelled, current?.surfaceId == surface.surfaceId else { return }
            wsManager.sendA2UIEvent(surfaceId: surface.surfaceId,
                                    event: "timeout", value: nil, values: [:])
            dismiss()
        }
    }

    private func dismiss() {
        timeoutTask?.cancel()
        withAnimation(.easeInOut(duration: 0.25)) {
            current = nil
            state = nil
        }
    }
}

extension View {
    /// Renders agent-pushed A2UI surfaces (approval prompts, enumerable CLARIFY).
    func a2uiOverlay() -> some View {
        modifier(A2UIOverlay())
    }
}
