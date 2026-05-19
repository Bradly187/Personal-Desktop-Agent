import SwiftUI

/// Guided tilt calibration flow presented as a sheet.
/// 1. Instruction: "Hold iPad in your comfortable position"
/// 2. Countdown: 3…2…1 with haptic ticks
/// 3. Auto-calibrate + success confirmation
struct TiltCalibrationSheet: View {
    @ObservedObject var sensorManager: SensorManager
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss) private var dismiss

    @State private var phase: CalibrationPhase = .ready
    @State private var countdown = 3

    private enum CalibrationPhase {
        case ready
        case counting
        case done
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer()

                // Animated icon
                Image(systemName: phaseIcon)
                    .font(.system(size: 64))
                    .foregroundStyle(phase == .done ? theme.success : theme.accent)
                    .symbolEffect(.pulse, isActive: phase == .counting)
                    .accessibilityHidden(true)

                // Title
                Text(phaseTitle)
                    .font(DesignTokens.Typography.headline)
                    .foregroundStyle(theme.textPrimary)
                    .multilineTextAlignment(.center)

                // Subtitle / countdown
                switch phase {
                case .ready:
                    Text("Hold your iPad in the position you'll normally use it. Keep it steady, then tap Start.")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textSecondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, DesignTokens.Spacing.xl)

                case .counting:
                    Text("\(countdown)")
                        .font(.system(size: 72, weight: .bold, design: .rounded))
                        .foregroundStyle(theme.accent)
                        .monospacedDigit()
                        .contentTransition(.numericText())
                        .animation(.easeInOut(duration: 0.3), value: countdown)

                    Text("Hold steady…")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textSecondary)

                case .done:
                    Text("Your neutral position is set. The cursor will center when you hold the iPad like this.")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textSecondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, DesignTokens.Spacing.xl)
                }

                Spacer()

                // Action button
                switch phase {
                case .ready:
                    Button {
                        startCountdown()
                    } label: {
                        Text("Start Calibration")
                            .font(DesignTokens.Typography.body.weight(.semibold))
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity)
                            .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                            .background(theme.accent)
                            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    }
                    .padding(.horizontal, DesignTokens.Spacing.xl)

                case .counting:
                    EmptyView()

                case .done:
                    Button {
                        dismiss()
                    } label: {
                        Text("Done")
                            .font(DesignTokens.Typography.body.weight(.semibold))
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity)
                            .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                            .background(theme.success)
                            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    }
                    .padding(.horizontal, DesignTokens.Spacing.xl)

                    Button("Retry") {
                        phase = .ready
                        countdown = 3
                    }
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.accent)
                }
            }
            .padding(.bottom, DesignTokens.Spacing.xl)
            .navigationTitle("Tilt Calibration")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    private var phaseIcon: String {
        switch phase {
        case .ready: return "level"
        case .counting: return "timer"
        case .done: return "checkmark.circle.fill"
        }
    }

    private var phaseTitle: String {
        switch phase {
        case .ready: return "Calibrate Neutral Position"
        case .counting: return "Hold Steady"
        case .done: return "Calibrated!"
        }
    }

    private func startCountdown() {
        phase = .counting
        countdown = 3

        Task { @MainActor in
            for i in stride(from: 3, through: 1, by: -1) {
                countdown = i
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            // Calibrate
            sensorManager.tiltSensor.calibrate()
            phase = .done
        }
    }
}
