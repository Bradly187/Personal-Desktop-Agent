import Combine
import SwiftUI

struct ContentView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var screenshotStore: ScreenshotStore

    @Environment(\.appTheme) private var theme

    @State private var selectedTab = 0

    /// The first N tabs participate in swipe-to-switch (page-style).
    /// Settings and Sensors are tap-only since they're utility views.
    private let swipeableTabCount = 4

    /// Tabs where page-swipe is disabled because the content owns horizontal gestures.
    /// Fix #1: Parent-driven scroll disable — no child hierarchy walking needed.
    private let swipeDisabledTabs: Set<Int> = [1, 3]  // Trackpad, Handwriting

    var body: some View {
        ZStack(alignment: .bottom) {

            // ── Active view ──────────────────────────────────────────────────
            activeView
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .safeAreaInset(edge: .bottom) {
                    Color.clear.frame(height: tabBarHeight)
                }

            // ── Overlays ─────────────────────────────────────────────────────
            DAConnectionBanner()
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.top, DesignTokens.Spacing.sm)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)

            // Sensor activity bar — shows running sensor icons
            SensorActivityBar()
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 44) // below connection banner

            // Cursor conflict warning
            CursorConflictBanner(settings: settings)
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 72) // below activity bar

            ScreenshotOverlayView()

            // ── Custom tab bar with drag-to-switch ───────────────────────────
            customTabBar
        }
        .ignoresSafeArea(.keyboard)
        .commandToast() // Last-command floating toast
        .onReceive(wsManager.messageStream) { message in
            if case .screenshot(_, let imageBase64, let mime) = message {
                screenshotStore.handleScreenshot(base64: imageBase64, mime: mime)
            }
        }
    }

    // MARK: - Active view (page-swipeable for first 4 tabs)

    @ViewBuilder
    private var activeView: some View {
        if selectedTab < swipeableTabCount {
            // TabView in page style — swipe left/right to slide between
            // Commands, Trackpad, Keypad, Write.
            // Fix #1: Overlay a scroll-disable bridge that the parent controls.
            TabView(selection: $selectedTab) {
                CommandPadView()
                    .tag(0)

                TrackpadView()
                    .tag(1)

                ScientificKeypadView()
                    .tag(2)

                HandwritingCanvasView()
                    .tag(3)
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .animation(.easeInOut(duration: 0.15), value: selectedTab) // Fix #13: reduced from 250ms
            .onChange(of: selectedTab) {
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
            }
            // Fix #1: Parent-driven scroll disable for gesture-heavy tabs
            .overlay(
                PageScrollDisabler(isDisabled: swipeDisabledTabs.contains(selectedTab))
                    .allowsHitTesting(false)
            )
        } else {
            // Non-swipeable utility tabs — direct switch
            switch selectedTab {
            case 4: SettingsView()
            case 5: SensorDashboardView()
            default: SettingsView()
            }
        }
    }

    // MARK: - Custom tab bar with drag-to-switch

    private let tabBarHeight: CGFloat = 80

    /// Accumulated drag translation for tab-bar swipe gesture.
    @State private var tabBarDragOffset: CGFloat = 0
    /// Threshold in points to trigger a tab switch via drag.
    private let dragThreshold: CGFloat = 60
    /// Fix #2: Track the tab value at drag start to prevent double-advance.
    @State private var tabAtDragStart: Int = 0

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
        .contentShape(Rectangle())
        .gesture(tabBarDragGesture)
    }

    /// Horizontal drag on the tab bar slides between tabs.
    /// Fix #2: Uses tabAtDragStart to prevent double-advance when page swipe also fires.
    private var tabBarDragGesture: some Gesture {
        DragGesture(minimumDistance: 20, coordinateSpace: .local)
            .onChanged { value in
                if tabBarDragOffset == 0 {
                    tabAtDragStart = selectedTab
                }
                tabBarDragOffset = value.translation.width
            }
            .onEnded { value in
                let totalTabs = 6
                // Only advance from the tab we started on (prevents double-advance)
                if value.translation.width < -dragThreshold {
                    let next = min(tabAtDragStart + 1, totalTabs - 1)
                    withAnimation(.easeInOut(duration: 0.2)) { selectedTab = next }
                } else if value.translation.width > dragThreshold {
                    let prev = max(tabAtDragStart - 1, 0)
                    withAnimation(.easeInOut(duration: 0.2)) { selectedTab = prev }
                }
                tabBarDragOffset = 0
            }
    }

    // MARK: - Tab button

    private func tabButton(_ index: Int, _ label: String, _ icon: String) -> some View {
        let isSelected = selectedTab == index
        return Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            withAnimation(.easeInOut(duration: 0.2)) { selectedTab = index }
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
        .buttonStyle(TabButtonStyle())
        .accessibilityLabel(label)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

// MARK: - Fix #1: Parent-driven page scroll disabler

/// UIViewRepresentable that finds the TabView's backing UIScrollView from the parent
/// and enables/disables its scroll based on the current tab. This avoids the fragile
/// child-walks-up-hierarchy pattern.
private struct PageScrollDisabler: UIViewRepresentable {
    let isDisabled: Bool

    func makeUIView(context: Context) -> PageScrollDisablerView {
        PageScrollDisablerView()
    }

    func updateUIView(_ uiView: PageScrollDisablerView, context: Context) {
        uiView.setScrollDisabled(isDisabled)
    }
}

private final class PageScrollDisablerView: UIView {
    private weak var cachedScrollView: UIScrollView?

    func setScrollDisabled(_ disabled: Bool) {
        if let sv = cachedScrollView, sv.window != nil {
            sv.isScrollEnabled = !disabled
            return
        }
        // Walk up to find the TabView's UIScrollView (it's a UICollectionView internally)
        var current: UIView? = superview
        while let view = current {
            if let scrollView = view as? UIScrollView {
                cachedScrollView = scrollView
                scrollView.isScrollEnabled = !disabled
                return
            }
            current = view.superview
        }
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        // Re-cache when view hierarchy changes
        cachedScrollView = nil
    }
}

// Dims the button while pressed so there's instant visual confirmation of a tap.
private struct TabButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? Color.primary.opacity(0.08) : Color.clear)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }
}
