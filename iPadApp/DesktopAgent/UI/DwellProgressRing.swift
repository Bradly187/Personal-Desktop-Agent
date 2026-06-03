import SwiftUI

/// Observes a `TiltSensor` and shows the dwell countdown ring while a tilt
/// dwell-to-click is in progress. Kept separate from `DwellProgressRing` so the
/// `@ObservedObject` dependency lives here (the parent ContentView only holds
/// the SensorManager and wouldn't otherwise re-render on the nested sensor's
/// `dwellInProgress` changes).
struct DwellRingHUD: View {
    @ObservedObject var tiltSensor: TiltSensor
    let duration: Double

    var body: some View {
        DwellProgressRing(isActive: tiltSensor.dwellInProgress, duration: duration)
    }
}

/// A radial countdown ring. When `isActive` flips true it fills from 0 → 1 over
/// `duration` seconds (matching the dwell hold), then disappears on fire/cancel.
/// Purely visual feedback — the click itself is fired PC-side.
struct DwellProgressRing: View {
    let isActive: Bool
    let duration: Double

    @Environment(\.appTheme) private var theme
    @State private var progress: Double = 0

    var body: some View {
        ZStack {
            Circle()
                .stroke(theme.textSecondary.opacity(0.25), lineWidth: 5)
            Circle()
                .trim(from: 0, to: progress)
                .stroke(theme.accent,
                        style: StrokeStyle(lineWidth: 5, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: 56, height: 56)
        .shadow(color: .black.opacity(0.2), radius: 4)
        .opacity(isActive ? 1 : 0)
        .onChange(of: isActive) { _, active in
            if active {
                progress = 0
                withAnimation(.linear(duration: duration)) { progress = 1 }
            } else {
                withAnimation(.easeOut(duration: 0.15)) { progress = 0 }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}
