import SwiftUI

struct ContentView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var screenshotStore: ScreenshotStore

    @Environment(\.appTheme) private var theme

    @State private var selectedTab = 0

    var body: some View {
        ZStack(alignment: .bottom) {

            // ── Active view ──────────────────────────────────────────────────
            // safeAreaInset reserves the tab bar height so scroll content
            // never hides under the bar.
            activeView
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .safeAreaInset(edge: .bottom) {
                    Color.clear.frame(height: tabBarHeight)
                }

            // ── Existing overlays (keep same behaviour as before) ────────────
            DAConnectionBanner()
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.top, DesignTokens.Spacing.sm)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)

            DwellToolbarContainer(settings: settings, ws: wsManager)

            ScreenshotOverlayView()

            // ── Custom tab bar — rendered last so always on top ──────────────
            customTabBar
        }
        .ignoresSafeArea(.keyboard)
        .onReceive(wsManager.$lastMessage) { message in
            guard let message else { return }
            if case .screenshot(_, let imageBase64, let mime) = message {
                screenshotStore.handleScreenshot(base64: imageBase64, mime: mime)
            }
        }
    }

    // MARK: - Active view

    @ViewBuilder
    private var activeView: some View {
        switch selectedTab {
        case 0: CommandPadView()
        case 1: TrackpadView()
        case 2: ScientificKeypadView()
        case 3: HandwritingCanvasView()
        case 4: SettingsView()
        case 5: LiDARDebugView()
        default: CommandPadView()
        }
    }

    // MARK: - Custom tab bar

    private let tabBarHeight: CGFloat = 80

    private var customTabBar: some View {
        HStack(spacing: 0) {
            tabButton(0, "Commands", "square.grid.3x3.fill")
            tabButton(1, "Trackpad", "hand.point.up")
            tabButton(2, "Keypad",   "function")
            tabButton(3, "Write",    "pencil.and.scribble")
            tabButton(4, "Settings", "gearshape.fill")
            tabButton(5, "Sensors",  "sensor.tag.radiowaves.forward")
        }
        .frame(height: tabBarHeight)
        .background(
            theme.surfacePrimary
                .shadow(color: .black.opacity(0.15), radius: 6, x: 0, y: -2)
                .ignoresSafeArea(edges: .bottom)
        )
    }

    private func tabButton(_ index: Int, _ label: String, _ icon: String) -> some View {
        let isSelected = selectedTab == index
        return Button {
            selectedTab = index
        } label: {
            VStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: icon)
                    .font(.system(size: 22, weight: isSelected ? .semibold : .regular))
                Text(label)
                    .font(DesignTokens.Typography.caption)
                    .lineLimit(1)
            }
            .foregroundStyle(isSelected ? theme.accent : theme.textSecondary)
            .frame(maxWidth: .infinity, minHeight: DesignTokens.Size.touchTargetMin)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}
