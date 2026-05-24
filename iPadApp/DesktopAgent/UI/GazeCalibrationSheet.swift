import SwiftUI

/// Guided gaze sensitivity auto-tune flow.
///
/// 1. "Look straight ahead for 3 seconds" — measures baseline jitter
/// 2. "Look left… right… up… down" — measures delta range
/// 3. Auto-computes optimal sensitivity and smoothing
/// 4. Shows results with accept/adjust option
struct GazeCalibrationSheet: View {
    @ObservedObject var sensorManager: SensorManager
    @ObservedObject var settings: SettingsStore

    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss) private var dismiss

    @State private var phase: GazeCalPhase = .intro
    @State private var direction: String = ""
    @State private var baselineJitter: Float = 0
    @State private var maxDeltaX: Float = 0
    @State private var maxDeltaY: Float = 0
    @State private var samples: [SIMD2<Float>] = []
    @State private var computedSensitivity: Double = 200
    @State private var computedSmoothing: Double = 0.04

    private enum GazeCalPhase {
        case intro
        case baseline    // look straight ahead
        case lookLeft
        case lookRight
        case lookUp
        case lookDown
        case computing
        case done
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer()

                phaseIcon
                phaseTitle
                phaseSubtitle

                Spacer()

                actionButton
            }
            .padding(DesignTokens.Spacing.xl)
            .navigationTitle("Gaze Calibration")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        sensorManager.gazeTracker.calibrationHandler = nil
                        dismiss()
                    }
                }
            }
        }
        .onDisappear {
            sensorManager.gazeTracker.calibrationHandler = nil
        }
    }

    // MARK: - Phase UI

    @ViewBuilder
    private var phaseIcon: some View {
        switch phase {
        case .intro:
            Image(systemName: "eye.circle")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)
        case .baseline:
            Image(systemName: "scope")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)
                .symbolEffect(.pulse, isActive: true)
        case .lookLeft:
            Image(systemName: "arrow.left.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)
        case .lookRight:
            Image(systemName: "arrow.right.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)
        case .lookUp:
            Image(systemName: "arrow.up.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)
        case .lookDown:
            Image(systemName: "arrow.down.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)
        case .computing:
            ProgressView()
                .controlSize(.large)
        case .done:
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.success)
        }
    }

    @ViewBuilder
    private var phaseTitle: some View {
        Text(titleText)
            .font(DesignTokens.Typography.headline)
            .foregroundStyle(theme.textPrimary)
            .multilineTextAlignment(.center)
    }

    @ViewBuilder
    private var phaseSubtitle: some View {
        Text(subtitleText)
            .font(DesignTokens.Typography.body)
            .foregroundStyle(theme.textSecondary)
            .multilineTextAlignment(.center)
            .padding(.horizontal, DesignTokens.Spacing.lg)
    }

    private var titleText: String {
        switch phase {
        case .intro: return "Auto-Tune Gaze Sensitivity"
        case .baseline: return "Look Straight Ahead"
        case .lookLeft: return "Look Left"
        case .lookRight: return "Look Right"
        case .lookUp: return "Look Up"
        case .lookDown: return "Look Down"
        case .computing: return "Calculating…"
        case .done: return "Calibration Complete"
        }
    }

    private var subtitleText: String {
        switch phase {
        case .intro: return "This takes about 15 seconds. We'll measure your eye movement range to set optimal sensitivity."
        case .baseline: return "Keep your eyes focused on the iPad screen. Measuring baseline jitter…"
        case .lookLeft: return "Move your eyes to the left (don't turn your head)."
        case .lookRight: return "Move your eyes to the right."
        case .lookUp: return "Look upward."
        case .lookDown: return "Look downward."
        case .computing: return "Analyzing your eye movement range…"
        case .done: return "Sensitivity: \(Int(computedSensitivity))  Smoothing: \(String(format: "%.2f", computedSmoothing))"
        }
    }

    // MARK: - Action Button

    @ViewBuilder
    private var actionButton: some View {
        switch phase {
        case .intro:
            Button {
                startCalibration()
            } label: {
                Text("Start")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }

        case .baseline, .lookLeft, .lookRight, .lookUp, .lookDown, .computing:
            EmptyView()

        case .done:
            VStack(spacing: DesignTokens.Spacing.md) {
                Button {
                    applyResults()
                    dismiss()
                } label: {
                    Text("Apply Settings")
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                        .background(theme.success)
                        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }
                HStack(spacing: DesignTokens.Spacing.md) {
                    Button("Retry") {
                        phase = .intro
                        samples = []
                        maxDeltaX = 0
                        maxDeltaY = 0
                        baselineJitter = 0
                    }
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.accent)

                    Text("·").foregroundStyle(theme.textSecondary)

                    // G9: reset to factory defaults if calibration produced bad values
                    Button("Reset to defaults") {
                        settings.gazeSensitivity = 200.0
                        settings.gazeStabilityThreshold = 0.04
                        dismiss()
                    }
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                }
            }
        }
    }

    // MARK: - Calibration Logic

    private func startCalibration() {
        // Ensure gaze is enabled and running
        if !settings.gazeEnabled {
            settings.gazeEnabled = true
        }

        phase = .baseline
        samples = []

        // Install calibration handler on GazeTracker
        sensorManager.gazeTracker.calibrationHandler = { [self] dx, dy in
            Task { @MainActor in
                self.samples.append(SIMD2(dx, dy))
            }
        }

        // Run the sequence
        Task { @MainActor in
            // Phase 1: Baseline (3 seconds)
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            baselineJitter = computeJitter(samples)
            samples = []

            // Phase 2: Look left (2 seconds)
            phase = .lookLeft
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            let leftRange = samples.map { abs($0.x) }.max() ?? 0
            maxDeltaX = max(maxDeltaX, leftRange)
            samples = []

            // Phase 3: Look right (2 seconds)
            phase = .lookRight
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            let rightRange = samples.map { abs($0.x) }.max() ?? 0
            maxDeltaX = max(maxDeltaX, rightRange)
            samples = []

            // Phase 4: Look up (2 seconds)
            phase = .lookUp
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            let upRange = samples.map { abs($0.y) }.max() ?? 0
            maxDeltaY = max(maxDeltaY, upRange)
            samples = []

            // Phase 5: Look down (2 seconds)
            phase = .lookDown
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            let downRange = samples.map { abs($0.y) }.max() ?? 0
            maxDeltaY = max(maxDeltaY, downRange)
            samples = []

            // Remove calibration handler
            sensorManager.gazeTracker.calibrationHandler = nil

            // Compute results
            phase = .computing
            computeOptimalSettings()
            phase = .done
        }
    }

    private func computeJitter(_ samples: [SIMD2<Float>]) -> Float {
        guard samples.count > 10 else { return 0.01 }
        let magnitudes = samples.map { sqrt($0.x * $0.x + $0.y * $0.y) }
        return magnitudes.reduce(0, +) / Float(magnitudes.count)
    }

    private func computeOptimalSettings() {
        // Target: full eye sweep should move cursor ~500px
        let maxDelta = max(maxDeltaX, maxDeltaY)
        guard maxDelta > 0.001 else {
            // Couldn't detect meaningful movement — use defaults
            computedSensitivity = 200
            computedSmoothing = 0.06
            return
        }

        // Sensitivity: 500px target / maxDelta per frame
        // maxDelta is the largest single-frame delta during a full eye sweep
        // We want that to produce ~15px of cursor movement per frame (smooth travel)
        computedSensitivity = Double(min(500, max(50, 15.0 / maxDelta)))

        // Smoothing: based on baseline jitter
        // Higher jitter → more smoothing needed
        if baselineJitter > 0.005 {
            computedSmoothing = 0.10  // heavy smoothing
        } else if baselineJitter > 0.002 {
            computedSmoothing = 0.06  // moderate
        } else {
            computedSmoothing = 0.03  // light
        }
    }

    private func applyResults() {
        settings.gazeSensitivity = computedSensitivity
        settings.gazeStabilityThreshold = computedSmoothing
    }
}
