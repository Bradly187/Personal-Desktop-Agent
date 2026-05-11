import SwiftUI

/// Multi-touch trackpad surface with click buttons below.
///
/// Layout (top to bottom):
///   1. Trackpad gesture area (takes all available space)
///   2. Click buttons row: Left Click, Right Click
///   3. Shortcut buttons row: Copy, Paste, Undo, Scroll ↓, Scroll ↑
///
/// The click buttons sit directly above the shortcut row so they're
/// easy to reach without lifting the hand far from the trackpad.
///
/// Gesture area:
///   Single-finger drag  → cursor move
///   Two-finger drag     → scroll
///   (Taps on the surface still work as before, but the explicit
///    buttons below are the primary click targets for low-effort use)
///
/// Palm rejection: touches with majorRadius > palmRejectRadius are ignored.
struct TrackpadView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore

    @State private var isFullScreen = false

    var body: some View {
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
    }

    // MARK: — Normal layout (trackpad + buttons)

    private var normalLayout: some View {
        VStack(spacing: 0) {
            // Trackpad gesture surface — fills available space
            trackpadSurface
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal, 12)
                .padding(.top, 8)

            // Click buttons row
            HStack(spacing: 12) {
                clickButton(label: "Left Click", icon: "cursorarrow.click", button: "left")
                clickButton(label: "Right Click", icon: "cursorarrow.click.2", button: "right")
                fullScreenButton
            }
            .padding(.horizontal, 12)
            .padding(.top, 10)

            // Shortcut buttons row (Copy, Paste, Undo, Scroll ↓, Scroll ↑)
            HStack(spacing: 10) {
                shortcutButton(label: "Copy", icon: "doc.on.doc", keys: ["ctrl", "c"])
                shortcutButton(label: "Paste", icon: "doc.on.clipboard", keys: ["ctrl", "v"])
                shortcutButton(label: "Undo", icon: "arrow.uturn.backward", keys: ["ctrl", "z"])
                scrollButton(label: "↓", direction: "down")
                scrollButton(label: "↑", direction: "up")
            }
            .padding(.horizontal, 12)
            .padding(.top, 8)
            .padding(.bottom, 12)
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
                            .padding(10)
                            .background(.regularMaterial, in: Circle())
                    }
                    .padding()
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
    }

    // MARK: — Button builders

    private func clickButton(label: String, icon: String, button: String) -> some View {
        Button {
            wsManager.sendTrackpadTap(button: button)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.body)
                Text(label)
                    .font(.subheadline.weight(.medium))
            }
            .frame(maxWidth: .infinity, minHeight: 56)
            .background(Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var fullScreenButton: some View {
        Button {
            withAnimation { isFullScreen = true }
        } label: {
            Image(systemName: "arrow.up.left.and.arrow.down.right")
                .font(.body)
                .frame(width: 56, height: 56)
                .background(Color(.secondarySystemGroupedBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func shortcutButton(label: String, icon: String, keys: [String]) -> some View {
        Button {
            wsManager.sendCommand(action: "HOTKEY", text: label, params: ["keys": keys])
        } label: {
            VStack(spacing: 2) {
                Image(systemName: icon)
                    .font(.callout)
                Text(label)
                    .font(.caption2)
            }
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(Color(.tertiarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func scrollButton(label: String, direction: String) -> some View {
        Button {
            wsManager.sendCommand(action: "SCROLL", text: "Scroll \(direction)",
                                  params: ["direction": direction])
        } label: {
            VStack(spacing: 2) {
                Image(systemName: direction == "down" ? "chevron.down" : "chevron.up")
                    .font(.callout)
                Text(label)
                    .font(.caption2)
            }
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(Color(.tertiarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: — Event handling

    private func handle(_ event: TrackpadEvent) {
        switch event {
        case .move(let dx, let dy):
            let s = settings.trackpadSpeed
            wsManager.sendTrackpadMove(dx: Int(dx * s), dy: Int(dy * s))
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
        }
    }
}

// MARK: — Event type

enum TrackpadEvent {
    case move(dx: CGFloat, dy: CGFloat)
    case tap(fingers: Int)
    case scroll(dx: CGFloat, dy: CGFloat)
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

        tap1.require(toFail: tap2)
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
            }
        }

        @objc func tap1(_ gr: UITapGestureRecognizer) {
            if gr.state == .recognized { onEvent(.tap(fingers: 1)) }
        }

        @objc func tap2(_ gr: UITapGestureRecognizer) {
            if gr.state == .recognized { onEvent(.tap(fingers: 2)) }
        }
    }
}
