import SwiftUI

/// Multi-touch trackpad surface.
/// Single-finger drag  → cursor move (sendTrackpadMove)
/// Single-finger tap   → left click
/// Two-finger tap      → right click
/// Two-finger drag     → scroll
/// Palm rejection: touches with majorRadius > palmRejectRadius are ignored.
struct TrackpadView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore

    @State private var isFullScreen = false

    var body: some View {
        ZStack {
            trackpadSurface
                .ignoresSafeArea(isFullScreen ? .all : [])

            if !isFullScreen {
                VStack {
                    Spacer()
                    HStack {
                        Spacer()
                        Button {
                            withAnimation { isFullScreen = true }
                        } label: {
                            Image(systemName: "arrow.up.left.and.arrow.down.right")
                                .padding(12)
                                .background(.regularMaterial, in: Circle())
                        }
                        .padding()
                    }
                }
            } else {
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
        .navigationTitle(isFullScreen ? "" : "Trackpad")
        .navigationBarHidden(isFullScreen)
    }

    private var trackpadSurface: some View {
        TrackpadGestureView(palmRadius: CGFloat(settings.palmRejectRadius),
                            speed: CGFloat(settings.trackpadSpeed)) { event in
            handle(event)
        }
        .background(Color(.systemGroupedBackground))
    }

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
            // Palm rejection
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
