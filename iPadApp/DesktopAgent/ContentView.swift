import Combine
import SwiftUI

struct ContentView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var screenshotStore: ScreenshotStore
    @EnvironmentObject var sensorManager: SensorManager

    @Environment(\.appTheme) private var theme

    @State private var selectedTab = 0
    @State private var recalRequest: RecalibrationRequest? = nil
    @State private var showRecalSheet = false

    /// The first N tabs participate in swipe-to-switch (page-style).
    /// Settings and Sensors are tap-only since they're utility views.
    private let swipeableTabCount = 3

    /// Tabs where page-swipe is disabled because the content owns horizontal gestures.
    /// Fix #1: Parent-driven scroll disable — no child hierarchy walking needed.
    private let swipeDisabledTabs: Set<Int> = [1, 2]  // Trackpad, Write

    var body: some View {
        ZStack(alignment: .bottom) {

            // ── Active view ──────────────────────────────────────────────────
            activeView
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .safeAreaInset(edge: .bottom) {
                    Color.clear.frame(height: tabBarHeight)
                }

            // ── Overlays ─────────────────────────────────────────────────────
            // Fix #16: Overlays use VStack with Spacer pattern so their frames
            // only cover the visible content area, not the full screen.
            // The Spacer has hit testing disabled so touches pass through to content below.
            DAConnectionBanner()
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.top, DesignTokens.Spacing.sm)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)

            // Sensor activity bar — shows running sensor icons (non-interactive)
            SensorActivityBar()
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 44) // below connection banner
                .allowsHitTesting(false)

            // Persistent mic-mute indicator/toggle — top-trailing, always
            // interactive. Safety-critical: the only way to unmute once muted.
            MicMuteIndicator()
                .padding(.trailing, DesignTokens.Spacing.lg)
                .padding(.top, DesignTokens.Spacing.sm)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)

            // Tilt dwell-to-click countdown ring — centered, non-interactive.
            // Observes TiltSensor directly so its dwellInProgress drives the ring.
            DwellRingHUD(tiltSensor: sensorManager.tiltSensor,
                         duration: settings.tiltDwellDuration)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
                .allowsHitTesting(false)

            ScreenshotOverlayView()

            // ── Custom tab bar with drag-to-switch ───────────────────────────
            customTabBar
        }
        .ignoresSafeArea(.keyboard)
        .commandToast() // Last-command floating toast
        .proactiveNotificationBanner() // PC-pushed briefs / reminders / alerts (N+2)
        .a2uiOverlay() // Agent-pushed A2UI surfaces (approval prompts, enumerable CLARIFY)
        .onReceive(wsManager.messageStream) { message in
            if case .screenshot(_, let imageBase64, let mime) = message {
                screenshotStore.handleScreenshot(base64: imageBase64, mime: mime)
            }
        }
        .onReceive(wsManager.recalibrationFeed) { request in
            recalRequest = request
            showRecalSheet = true
        }
        .sheet(isPresented: $showRecalSheet) {
            if let req = recalRequest {
                QuickRecalSheet(
                    settings: settings,
                    reason: req.reason,
                    degradationPct: req.degradationPct
                )
            }
        }
    }

    // MARK: - Active view (page-swipeable for first 4 tabs)

    @ViewBuilder
    private var activeView: some View {
        // Fix #15: Keep TabView alive across all tabs to prevent UIScrollView
        // destruction/recreation when switching to Settings/Sensors and back.
        // This fixes the PageScrollDisabler losing its cached reference.
        ZStack {
            TabView(selection: $selectedTab) {
                CommandPadView()
                    .tag(0)

                TrackpadView()
                    .tag(1)

                HandwritingCanvasView()
                    .tag(2)
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .animation(.easeInOut(duration: 0.15), value: selectedTab)
            .onChange(of: selectedTab) {
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
            }
            .overlay(
                PageScrollDisabler(isDisabled: swipeDisabledTabs.contains(selectedTab))
                    .allowsHitTesting(false)
            )
            .opacity(selectedTab < swipeableTabCount ? 1 : 0)
            .allowsHitTesting(selectedTab < swipeableTabCount)

            // Non-swipeable utility tabs — shown on top when active
            if selectedTab >= swipeableTabCount {
                switch selectedTab {
                case 3: SettingsView()
                case 4: SensorDashboardView()
                default: SettingsView()
                }
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
            tabButton(2, "Write",    "pencil.and.scribble")
            tabButton(3, "Settings", "gearshape.fill")
            tabButton(4, "Sensors",  "sensor.tag.radiowaves.forward")
        }
        .frame(height: tabBarHeight)
        .background(
            theme.surfacePrimary
                .shadow(color: .black.opacity(0.15), radius: 6, x: 0, y: -2)
                .ignoresSafeArea(edges: .bottom)
        )
        // Fix #14: simultaneousGesture lets button taps fire without waiting for
        // drag failure. minimumDistance: 40 ensures slight finger jitter during
        // taps (common with arthritis) doesn't accidentally trigger tab switching.
        .simultaneousGesture(tabBarDragGesture)
    }

    /// Horizontal drag on the tab bar slides between tabs.
    /// Fix #2: Uses tabAtDragStart to prevent double-advance when page swipe also fires.
    /// Fix #14: Raised minimumDistance to 40pt to prevent jittery taps from triggering drag.
    private var tabBarDragGesture: some Gesture {
        DragGesture(minimumDistance: 40, coordinateSpace: .local)
            .onChanged { value in
                if tabBarDragOffset == 0 {
                    tabAtDragStart = selectedTab
                }
                tabBarDragOffset = value.translation.width
            }
            .onEnded { value in
                let totalTabs = 5
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
        // Validate cached reference is still in the hierarchy
        if let sv = cachedScrollView, sv.window != nil {
            sv.isScrollEnabled = !disabled
            return
        }
        // Cache miss — walk up to find the TabView's UIScrollView
        cachedScrollView = nil
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

    override func didMoveToSuperview() {
        super.didMoveToSuperview()
        // Invalidate cache when re-parented (e.g. TabView recreated)
        cachedScrollView = nil
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
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
