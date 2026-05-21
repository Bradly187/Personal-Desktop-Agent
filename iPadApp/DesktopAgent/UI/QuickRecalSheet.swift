import SwiftUI
import Combine

/// Quick voice re-calibration — triggered automatically when the PC detects
/// that voice clarity has dropped ≥30% from the stored baseline.
///
/// Shorter than the full VoiceProfilingSheet: 3 core phrases × 3 repeats
/// ≈ 90 seconds.  On completion, the PC's AcousticProfiler calls
/// mark_calibrated() to reset the drift baseline.
struct QuickRecalSheet: View {

    @ObservedObject var settings: SettingsStore
    let reason: String          // "voice_clarity" | "seasonal"
    let degradationPct: Double  // 0–100, shown to user

    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss)  private var dismiss

    // Subset of full phrase bank — highest-coverage verbs
    private let phrases: [(text: String, hint: String)] = [
        ("hey agent",   "Your wake phrase"),
        ("click",       "Short and crisp"),
        ("scroll down", "Two clear words"),
    ]

    private let repeatsPerPhrase = 3
    private let secondsPerRepeat: Int = 4

    @State private var phase: Phase = .intro
    @State private var phraseIndex  = 0
    @State private var repeatIndex  = 0
    @State private var countdown    = 0
    @State private var timer: AnyCancellable?

    private enum Phase { case intro, speaking, done }

    // MARK: - Body

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer()
                switch phase {
                case .intro:    introContent
                case .speaking: speakingContent
                case .done:     doneContent
                }
                Spacer()
                actionButton
            }
            .padding(DesignTokens.Spacing.xl)
            .navigationTitle("Quick Voice Check")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Later") { dismiss() }
                }
            }
        }
        .onDisappear { timer?.cancel() }
    }

    // MARK: - Intro

    private var introContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            // Context-aware icon
            Image(systemName: reason == "seasonal"
                  ? "calendar.badge.exclamationmark"
                  : "waveform.badge.exclamationmark")
                .font(.system(size: 64))
                .foregroundStyle(theme.warning)

            Text(reason == "seasonal"
                 ? "Monthly Voice Check"
                 : "Voice Drift Detected")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            if reason == "voice_clarity" && degradationPct > 0 {
                Text("Your voice confidence has dropped \(Int(degradationPct))% from your baseline. This happens naturally on harder days — a quick 90-second check will re-tune the system.")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
            } else {
                Text("It's been about 30 days since your last voice profile. A quick check keeps recognition accurate as your voice naturally shifts over time.")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
            }

            HStack(spacing: DesignTokens.Spacing.xl) {
                statBadge("3", "phrases")
                statBadge("3×", "each")
                statBadge("~90s", "total")
            }
        }
    }

    private func statBadge(_ value: String, _ label: String) -> some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.system(size: 28, weight: .bold, design: .rounded))
                .foregroundStyle(theme.accent)
            Text(label)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
        }
        .frame(maxWidth: .infinity)
        .padding(DesignTokens.Spacing.md)
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
    }

    // MARK: - Speaking (reuses VoiceProfilingSheet layout)

    private var speakingContent: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {

            let totalRepeats = phrases.count * repeatsPerPhrase
            let done = phraseIndex * repeatsPerPhrase + repeatIndex
            ProgressView(value: Double(done), total: Double(totalRepeats))
                .tint(theme.accent)
            Text("Phrase \(phraseIndex + 1) of \(phrases.count)")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            HStack(spacing: DesignTokens.Spacing.sm) {
                ForEach(0..<repeatsPerPhrase, id: \.self) { i in
                    Circle()
                        .fill(i < repeatIndex ? theme.success :
                              i == repeatIndex ? theme.accent :
                              theme.surfaceTertiary)
                        .frame(width: 14, height: 14)
                        .animation(.easeInOut(duration: 0.2), value: repeatIndex)
                }
            }

            let phrase = phrases[phraseIndex]
            Text(phrase.text)
                .font(.system(size: 40, weight: .bold, design: .rounded))
                .foregroundStyle(theme.textPrimary)
                .multilineTextAlignment(.center)

            Text(phrase.hint)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            // Countdown ring
            ZStack {
                Circle()
                    .stroke(theme.surfaceSecondary, lineWidth: 6)
                    .frame(width: 80, height: 80)
                Circle()
                    .trim(from: 0, to: countdown > 0
                          ? CGFloat(countdown) / CGFloat(secondsPerRepeat) : 0)
                    .stroke(theme.accent,
                            style: StrokeStyle(lineWidth: 6, lineCap: .round))
                    .frame(width: 80, height: 80)
                    .rotationEffect(.degrees(-90))
                    .animation(.linear(duration: 1), value: countdown)
                Image(systemName: "mic.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(countdown > 0 ? theme.accent : theme.success)
                    .symbolEffect(.variableColor.iterative, isActive: countdown > 0)
            }
        }
    }

    // MARK: - Done

    private var doneContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.success)

            Text("All Done!")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("Voice baseline updated. The system will apply the new thresholds within a few seconds.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            // Update last calibration date in settings
            Color.clear.frame(height: 0).onAppear {
                settings.lastCalibrationDate = Date()
            }
        }
    }

    // MARK: - Action button

    @ViewBuilder
    private var actionButton: some View {
        switch phase {
        case .intro:
            Button { start() } label: {
                Label("Start Quick Check", systemImage: "mic.fill")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }

        case .speaking:
            Button { skip() } label: {
                Text("Skip phrase")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            }

        case .done:
            Button { dismiss() } label: {
                Text("Done")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.success)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }
        }
    }

    // MARK: - Logic

    private func start() {
        if !settings.audioStreamEnabled { settings.audioStreamEnabled = true }
        phase = .speaking
        phraseIndex = 0
        repeatIndex = 0
        startCountdown()
    }

    private func startCountdown() {
        countdown = secondsPerRepeat
        timer?.cancel()
        timer = Timer.publish(every: 1, on: .main, in: .common)
            .autoconnect()
            .sink { _ in
                if countdown > 1 { countdown -= 1 }
                else { timer?.cancel(); advanceRepeat() }
            }
    }

    private func advanceRepeat() {
        let next = repeatIndex + 1
        if next < repeatsPerPhrase {
            repeatIndex = next
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { startCountdown() }
        } else {
            advancePhrase()
        }
    }

    private func advancePhrase() {
        let next = phraseIndex + 1
        if next < phrases.count {
            phraseIndex = next
            repeatIndex = 0
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { startCountdown() }
        } else {
            phase = .done
        }
    }

    private func skip() {
        timer?.cancel()
        advancePhrase()
    }
}
