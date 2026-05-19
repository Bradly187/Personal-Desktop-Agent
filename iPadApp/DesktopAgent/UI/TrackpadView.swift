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

    // MARK: — Fractional accumulator (prevents Int truncation from dropping small moves)
    @State private var accumX: CGFloat = 0
    @State private var accumY: CGFloat = 0

    // MARK: — Event handling

    private func handle(_ event: TrackpadEvent) {
        switch event {
        case .move(let dx, let dy):
            let s = settings.trackpadSpeed
            accumX += dx * s
            accumY += dy * s

            let ix = Int(accumX)
            let iy = Int(accumY)

            if ix != 0 || iy != 0 {
                accumX -= CGFloat(ix)
                accumY -= CGFloat(iy)
                wsManager.sendTrackpadMove(dx: ix, dy: iy)
            }
        case .tap(let fingers):
            if fingers == 1 {
                wsManager.sendTrackpadTap(button: "left")
            } else if fingers == 2 {
                wsManager.sendTrackpadTap(button: "right")
            }
        case .scroll(let dx, let dy):
            if abs(dy) >= abs(dx) {
                wsManager.sendTrackpadScroll(direction: dy < 0 ? "up" : "down")
            } else {
                wsManager.sendTrackpadScroll(direction: dx < 0 ? "left" : "right")
            }
        case .ended:
            accumX = 0
            accumY = 0
        }
    }
}

// MARK: — Event type

enum TrackpadEvent {
    case move(dx: CGFloat, dy: CGFloat)
    case tap(fingers: Int)
    case scroll(dx: CGFloat, dy: CGFloat)
    case ended
}

// MARK: — UIViewRepresentable gesture engine

struct TrackpadGestureView: UIViewRepresentable {
    let palmRadius: CGFloat
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

    func updateUIView(_ uiView: UIView, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(palmRadius: palmRadius, onEvent: onEvent)
    }

    // MARK: — Coordinator

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var palmRadius: CGFloat
        let onEvent: (TrackpadEvent) -> Void
        weak var view: UIView?
        private var prevTranslation: CGPoint = .zero
        /// Timestamp of last 2-finger tap — used to suppress the spurious 1-finger tap
        /// that fires alongside it when require(toFail:) is removed.
        private var lastTap2Time: CFTimeInterval = 0

        init(palmRadius: CGFloat, onEvent: @escaping (TrackpadEvent) -> Void) {
            self.palmRadius = palmRadius
            self.onEvent = onEvent
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
                prevTranslation = t
            case .changed:
                let dx = t.x - prevTranslation.x
                let dy = t.y - prevTranslation.y
                prevTranslation = t

                if gr.numberOfTouches == 2 {
                    onEvent(.scroll(dx: dx, dy: dy))
                } else {
                    onEvent(.move(dx: dx, dy: dy))
                }
            default:
                prevTranslation = .zero
                onEvent(.ended)
            }
        }

        @objc func tap1(_ gr: UITapGestureRecognizer) {
            guard gr.state == .recognized else { return }
            // Suppress if a 2-finger tap just fired (within 100ms)
            if CACurrentMediaTime() - lastTap2Time < 0.1 { return }
            onEvent(.tap(fingers: 1))
        }

        @objc func tap2(_ gr: UITapGestureRecognizer) {
            if gr.state == .recognized {
                lastTap2Time = CACurrentMediaTime()
                onEvent(.tap(fingers: 2))
            }
        }
    }
}
