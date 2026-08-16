import SwiftUI
import QuartzCore

/// Multi-touch trackpad surface with click buttons below.
///
/// Layout (top to bottom):
///   1. Trackpad gesture area (takes all available space)
///   2. Click buttons row: Left Click, Right Click
///   3. Shortcut buttons row: Copy, Paste, Undo, Scroll ↓, Scroll ↑
///
/// Click buttons use DesignTokens.Size.touchTargetMin (80pt) minimum.
/// Shortcut buttons use DesignTokens.Size.touchTargetCompact (64pt) minimum.
///
/// Gesture area:
///   Single-finger drag  → cursor move
///   Two-finger drag     → scroll
///
/// Palm rejection: touches with majorRadius > palmRejectRadius are ignored.
struct TrackpadView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore

    @Environment(\.appTheme) private var theme

    @State private var isFullScreen = false

    var body: some View {
        NavigationStack {
            ZStack {
                if isFullScreen {
                    fullScreenLayout
                        .ignoresSafeArea(.all)
                } else {
                    normalLayout
                }
            }
            .navigationTitle(isFullScreen ? "" : "Trackpad")
            .navigationBarHidden(isFullScreen)
            .toolbar(isFullScreen ? .hidden : .visible, for: .tabBar)
        }
    }

    // MARK: — Normal layout (trackpad + buttons)

    private var normalLayout: some View {
        VStack(spacing: 0) {
            // Trackpad gesture surface — fills available space
            trackpadSurface
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .padding(.horizontal, DesignTokens.Spacing.md)
                .padding(.top, DesignTokens.Spacing.sm)

            // Click buttons row (80pt minimum touch targets)
            HStack(spacing: DesignTokens.Spacing.md) {
                clickButton(label: "Left Click", icon: "cursorarrow.click", button: "left")
                clickButton(label: "Right Click", icon: "cursorarrow.click.2", button: "right")
                fullScreenButton
            }
            .padding(.horizontal, DesignTokens.Spacing.md)
            .padding(.top, DesignTokens.Spacing.md)

            // Shortcut buttons row (64pt compact minimum)
            HStack(spacing: DesignTokens.Spacing.sm) {
                shortcutButton(label: "Copy", icon: "doc.on.doc", keys: ["ctrl", "c"])
                shortcutButton(label: "Paste", icon: "doc.on.clipboard", keys: ["ctrl", "v"])
                shortcutButton(label: "Undo", icon: "arrow.uturn.backward", keys: ["ctrl", "z"])
                scrollButton(label: "↓", direction: "down")
                scrollButton(label: "↑", direction: "up")
            }
            .padding(.horizontal, DesignTokens.Spacing.md)
            .padding(.top, DesignTokens.Spacing.sm)
            .padding(.bottom, DesignTokens.Spacing.md)
        }
    }

    // MARK: — Full-screen layout (trackpad only, collapse button)

    private var fullScreenLayout: some View {
        ZStack {
            trackpadSurface

            VStack {
                HStack {
                    Button {
                        withAnimation { isFullScreen = false }
                    } label: {
                        Image(systemName: "arrow.down.right.and.arrow.up.left")
                            .padding(DesignTokens.Spacing.md)
                            .background(.regularMaterial, in: Circle())
                    }
                    .accessibilityLabel("Exit full screen")
                    .padding(DesignTokens.Spacing.lg)
                    Spacer()
                }
                Spacer()
            }
        }
    }

    // MARK: — Trackpad surface

    private var trackpadSurface: some View {
        TrackpadGestureView(palmRadius: CGFloat(settings.palmRejectRadius),
                            speed: CGFloat(settings.trackpadSpeed)) { event in
            handle(event)
        }
        .background(Color(.systemGroupedBackground))
        .accessibilityLabel("Trackpad surface")
        .accessibilityHint("Drag to move cursor, two-finger drag to scroll")
    }

    // MARK: — Button builders

    private func clickButton(label: String, icon: String, button: String) -> some View {
        Button {
            wsManager.sendTrackpadTap(button: button)
        } label: {
            HStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: icon)
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.accent)
                Text(label)
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textPrimary)
            }
            .frame(maxWidth: .infinity,
                   minHeight: DesignTokens.Size.touchTargetMin)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityHint("Double-tap to \(button) click")
    }

    private var fullScreenButton: some View {
        Button {
            withAnimation { isFullScreen = true }
        } label: {
            Image(systemName: "arrow.up.left.and.arrow.down.right")
                .font(.system(size: DesignTokens.Size.iconSize))
                .foregroundStyle(theme.accent)
                .frame(width: DesignTokens.Size.touchTargetMin,
                       height: DesignTokens.Size.touchTargetMin)
                .background(theme.surfaceSecondary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Full screen trackpad")
        .accessibilityHint("Double-tap to expand trackpad to full screen")
    }

    private func shortcutButton(label: String, icon: String, keys: [String]) -> some View {
        Button {
            wsManager.sendCommand(action: "HOTKEY", text: label, params: ["keys": keys])
        } label: {
            VStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: icon)
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.accent)
                Text(label)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
            }
            .frame(maxWidth: .infinity,
                   minHeight: DesignTokens.Size.touchTargetCompact)
            .background(theme.surfaceTertiary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityHint("Double-tap to send \(label) shortcut")
    }

    private func scrollButton(label: String, direction: String) -> some View {
        Button {
            wsManager.sendCommand(action: "SCROLL", text: "Scroll \(direction)",
                                  params: ["direction": direction])
        } label: {
            VStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: direction == "down" ? "chevron.down" : "chevron.up")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.accent)
                Text(label)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
            }
            .frame(maxWidth: .infinity,
                   minHeight: DesignTokens.Size.touchTargetCompact)
            .background(theme.surfaceTertiary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Scroll \(direction)")
        .accessibilityHint("Double-tap to scroll \(direction)")
    }

    // MARK: — Event handling

    /// Thin dispatcher. All accumulation, speed scaling, and rate limiting now
    /// live in `TrackpadGestureView.Coordinator` (R10.1) — events arrive here
    /// already reduced to whole units, so this never touches `@State` and the
    /// view graph is not invalidated on every gesture frame (R10.2).
    private func handle(_ event: TrackpadEvent) {
        switch event {
        case .move(let dx, let dy):
            wsManager.sendTrackpadMove(dx: dx, dy: dy)
        case .tap(let fingers):
            wsManager.sendTrackpadTap(button: fingers == 2 ? "right" : "left")
        case .scroll(let direction, let clicks):
            wsManager.sendTrackpadScroll(direction: direction, clicks: clicks)
        case .ended:
            break
        }
    }
}

// MARK: — Event type

/// Events emitted by the gesture coordinator, already reduced to whole units.
enum TrackpadEvent: Equatable {
    /// Whole-pixel cursor delta, speed already applied.
    case move(dx: Int, dy: Int)
    case tap(fingers: Int)
    /// Compass direction plus a positive magnitude in scroll clicks.
    case scroll(direction: String, clicks: Int)
    case ended
}

// MARK: — UIViewRepresentable gesture engine

struct TrackpadGestureView: UIViewRepresentable {
    let palmRadius: CGFloat
    /// Cursor speed multiplier. Applied inside the Coordinator alongside the
    /// fractional accumulator (R10.1) — which is why it is a real dependency
    /// here rather than the dead property it used to be.
    let speed: CGFloat
    let onEvent: (TrackpadEvent) -> Void

    func makeUIView(context: Context) -> UIView {
        let v = UIView()
        v.backgroundColor = .clear
        let c = context.coordinator
        c.view = v

        let pan = UIPanGestureRecognizer(target: c, action: #selector(Coordinator.pan(_:)))
        pan.minimumNumberOfTouches = 1
        pan.maximumNumberOfTouches = 2
        pan.delegate = c
        v.addGestureRecognizer(pan)
        c.panRecognizer = pan
        // Once the view is in the window, make the enclosing TabView page-swiper
        // wait for this pan to fail before it can fire (see linkAncestorScrollViews).
        DispatchQueue.main.async { c.linkAncestorScrollViewsIfNeeded() }

        let tap1 = UITapGestureRecognizer(target: c, action: #selector(Coordinator.tap1(_:)))
        tap1.numberOfTouchesRequired = 1
        v.addGestureRecognizer(tap1)

        let tap2 = UITapGestureRecognizer(target: c, action: #selector(Coordinator.tap2(_:)))
        tap2.numberOfTouchesRequired = 2
        v.addGestureRecognizer(tap2)

        // No require(toFail:) — eliminates the ~300ms delay on single-finger taps.
        // The coordinator handles the overlap: a 2-finger tap fires tap2 and we
        // suppress the accompanying tap1 via a short debounce window.
        tap2.delaysTouchesEnded = false
        return v
    }

    func updateUIView(_ uiView: UIView, context: Context) {
        // Push live settings into the coordinator on every update. Without this
        // the coordinator keeps the palmRadius captured at makeCoordinator()
        // forever — and because ContentView deliberately keeps the TabView alive
        // (its "Fix #15"), this view is never recreated, so the Settings
        // "Palm Reject Radius" slider stayed inert for the whole session.
        Self.applyLiveSettings(to: context.coordinator,
                               palmRadius: palmRadius,
                               speed: speed,
                               onEvent: onEvent)

        // Retry the scroll-view link here too: on the first makeUIView the view
        // may not yet be in a window, so the ancestor page scroll view isn't
        // reachable. updateUIView runs after insertion / on layout.
        context.coordinator.linkAncestorScrollViewsIfNeeded()
    }

    /// Pushes user-tunable settings and the current event sink into a live
    /// coordinator.
    ///
    /// `onEvent` is refreshed here too (R10.3): the coordinator is created once
    /// and outlives every `body` evaluation, so holding the closure captured at
    /// `makeCoordinator()` is the same stale-capture defect that made the palm
    /// radius slider inert. Refreshing every property in one place closes the
    /// whole class of bug rather than the one instance of it.
    ///
    /// `static` so it is testable without a `UIViewRepresentableContext`, which
    /// cannot be constructed outside SwiftUI — same testability seam as
    /// `WebSocketManager.frameBinaryAudio` / `appendingToken`.
    static func applyLiveSettings(to coordinator: Coordinator,
                                  palmRadius: CGFloat,
                                  speed: CGFloat? = nil,
                                  onEvent: ((TrackpadEvent) -> Void)? = nil) {
        coordinator.palmRadius = palmRadius
        if let speed { coordinator.speed = speed }
        if let onEvent { coordinator.onEvent = onEvent }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(palmRadius: palmRadius, speed: speed, onEvent: onEvent)
    }

    // MARK: — Coordinator

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var palmRadius: CGFloat
        var speed: CGFloat
        var onEvent: (TrackpadEvent) -> Void
        weak var view: UIView?
        weak var panRecognizer: UIPanGestureRecognizer?
        private var prevTranslation: CGPoint = .zero
        /// Set once we've made the ancestor page scroll view(s) defer to our pan.
        private var didLinkScrollViews = false

        // MARK: — Tuning (R8.2, R9.3)

        /// How long a 1-finger tap is held back waiting to see whether it is
        /// really the first half of a 2-finger tap. Well under the ~300 ms that
        /// `tap1.require(toFail: tap2)` would cost.
        var tapDisambiguationWindow: TimeInterval = 0.08
        /// Gesture points of two-finger travel per emitted scroll click.
        var scrollPointsPerClick: CGFloat = 20
        /// Ceiling on scroll emissions per second. Excess is coalesced into the
        /// next emission, never dropped.
        var maxScrollMessagesPerSecond: Double = 30

        // MARK: — Interaction state (R10.1 — owned here, not in @State)

        /// Fractional cursor accumulator. Prevents `Int` truncation from
        /// swallowing slow movements.
        private var accumX: CGFloat = 0
        private var accumY: CGFloat = 0
        /// Fractional scroll accumulator — same technique, applied to the scroll
        /// path so magnitude survives instead of being pinned to a constant.
        private var scrollAccumX: CGFloat = 0
        private var scrollAccumY: CGFloat = 0
        private var lastScrollEmit: CFTimeInterval = 0

        /// Timestamp of the last 2-finger tap, and the deferred 1-finger tap
        /// awaiting disambiguation. Together these cover *both* recognizer
        /// orderings (R8.1) — the old code only handled tap2-before-tap1.
        private var lastTap2Time: CFTimeInterval = 0
        private var pendingTap1: DispatchWorkItem?

        init(palmRadius: CGFloat, speed: CGFloat, onEvent: @escaping (TrackpadEvent) -> Void) {
            self.palmRadius = palmRadius
            self.speed = speed
            self.onEvent = onEvent
        }

        /// Walk up from the trackpad view and require every ancestor `UIScrollView`'s
        /// pan (the TabView `.page` swiper) to fail before it fires. The trackpad's
        /// own pan recognizes any 1–2 finger drag on the surface, so it always wins
        /// and the page never swipes while the user is moving the cursor — while a
        /// swipe that starts *outside* the trackpad still pages normally. Idempotent
        /// and self-healing: no-ops until the view is in a window, retried until at
        /// least one ancestor scroll view is found.
        func linkAncestorScrollViewsIfNeeded() {
            guard !didLinkScrollViews,
                  let view, view.window != nil,
                  let pan = panRecognizer else { return }
            var current: UIView? = view.superview
            var linked = false
            while let v = current {
                if let scrollView = v as? UIScrollView {
                    scrollView.panGestureRecognizer.require(toFail: pan)
                    linked = true
                }
                current = v.superview
            }
            if linked { didLinkScrollViews = true }
        }

        func gestureRecognizer(_ gr: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            return touch.majorRadius < palmRadius
        }

        func gestureRecognizer(_ a: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith b: UIGestureRecognizer) -> Bool {
            false
        }

        @objc func pan(_ gr: UIPanGestureRecognizer) {
            let t = gr.translation(in: view)
            switch gr.state {
            case .began:
                // Fallback in case the view wasn't in a window during make/update.
                linkAncestorScrollViewsIfNeeded()
                prevTranslation = t
            case .changed:
                let dx = t.x - prevTranslation.x
                let dy = t.y - prevTranslation.y
                prevTranslation = t

                if gr.numberOfTouches == 2 {
                    accumulateScroll(dx: dx, dy: dy)
                } else {
                    accumulateMove(dx: dx, dy: dy)
                }
            case .ended, .cancelled, .failed:
                endGesture()
            default:
                break
            }
        }

        // MARK: — Move / scroll accumulation

        /// Applies speed, accumulates the fractional remainder, and emits only
        /// whole pixels. Sub-pixel motion is carried forward rather than lost.
        func accumulateMove(dx: CGFloat, dy: CGFloat) {
            accumX += dx * speed
            accumY += dy * speed
            let ix = Int(accumX)
            let iy = Int(accumY)
            guard ix != 0 || iy != 0 else { return }
            accumX -= CGFloat(ix)
            accumY -= CGFloat(iy)
            onEvent(.move(dx: ix, dy: iy))
        }

        /// Accumulates two-finger travel and emits rate-limited scroll events
        /// carrying real magnitude (R9.1–R9.3). Previously every `.changed`
        /// callback sent a fixed `clicks: 3` — up to 120 messages/second on
        /// ProMotion, none of them eligible for backpressure dropping.
        func accumulateScroll(dx: CGFloat, dy: CGFloat,
                              now: CFTimeInterval = CACurrentMediaTime()) {
            scrollAccumX += dx
            scrollAccumY += dy
            guard now - lastScrollEmit >= 1.0 / maxScrollMessagesPerSecond else { return }
            emitScrollIfWhole(now: now)
        }

        /// Converts accumulated travel into whole clicks along the dominant axis
        /// and emits. Diagonal scroll is deliberately out of scope — the PC's
        /// `mouse_scroll` takes a compass direction, so two-axis scroll would be
        /// a protocol change.
        private func emitScrollIfWhole(now: CFTimeInterval) {
            let vertical = abs(scrollAccumY) >= abs(scrollAccumX)
            let travel = vertical ? scrollAccumY : scrollAccumX
            let clicks = Int(travel / scrollPointsPerClick)
            // R9.5 — below one whole click, keep accumulating rather than
            // emitting a zero-magnitude message.
            guard clicks != 0 else { return }

            let consumed = CGFloat(clicks) * scrollPointsPerClick
            if vertical {
                scrollAccumY -= consumed
                scrollAccumX = 0
            } else {
                scrollAccumX -= consumed
                scrollAccumY = 0
            }
            let direction: String = vertical
                ? (travel < 0 ? "up" : "down")
                : (travel < 0 ? "left" : "right")
            lastScrollEmit = now
            onEvent(.scroll(direction: direction, clicks: abs(clicks)))
        }

        /// Flushes any remaining whole-click scroll travel, then clears all
        /// interaction state. Flushing keeps total scrolled distance
        /// proportional to total gesture displacement (R9.4).
        func endGesture(now: CFTimeInterval = CACurrentMediaTime()) {
            emitScrollIfWhole(now: now)
            prevTranslation = .zero
            accumX = 0
            accumY = 0
            scrollAccumX = 0
            scrollAccumY = 0
            onEvent(.ended)
        }

        // MARK: — Tap disambiguation (R8)

        @objc func tap1(_ gr: UITapGestureRecognizer) {
            guard gr.state == .recognized else { return }
            onSingleTapRecognized()
        }

        /// A 1-finger tap was recognized. It may be a genuine left click, or it
        /// may be one half of a 2-finger tap whose second recognizer has not
        /// reported yet — UIKit guarantees no ordering between them.
        ///
        /// Both orderings are handled: if tap2 already fired we drop this
        /// outright; otherwise we defer briefly so a tap2 still to come can
        /// cancel us. The old code only did the former, so fingers landing
        /// slightly apart — likelier with tremor — emitted a stray left click
        /// before the intended right click.
        func onSingleTapRecognized(now: CFTimeInterval = CACurrentMediaTime()) {
            // Ordering A: tap2 already fired — this is its straggler.
            if now - lastTap2Time < tapDisambiguationWindow { return }

            // Ordering B: tap2 may still be coming. Hold briefly.
            pendingTap1?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.pendingTap1 = nil
                self.onEvent(.tap(fingers: 1))
            }
            pendingTap1 = work
            DispatchQueue.main.asyncAfter(deadline: .now() + tapDisambiguationWindow,
                                          execute: work)
        }

        @objc func tap2(_ gr: UITapGestureRecognizer) {
            guard gr.state == .recognized else { return }
            onDoubleTapRecognized()
        }

        /// A 2-finger tap was recognized. Cancels any 1-finger tap still being
        /// held for disambiguation (ordering B) and stamps the clock so a
        /// straggler 1-finger recognition is dropped (ordering A).
        func onDoubleTapRecognized(now: CFTimeInterval = CACurrentMediaTime()) {
            lastTap2Time = now
            pendingTap1?.cancel()
            pendingTap1 = nil
            onEvent(.tap(fingers: 2))
        }

        deinit {
            pendingTap1?.cancel()
        }
    }
}
