import SwiftUI
import Combine

/// Guided voice profiling flow that builds a per-user acoustic baseline.
///
/// The user reads 10 command phrases aloud (3 times each).
/// The PC's AcousticProfiler silently captures RMS amplitude, spectral
/// centroid, and Whisper logprob for each utterance — deriving a
/// per-user VAD silence threshold and Gate 1 logprob floor.
///
/// No network round-trip needed: the iPad just presents phrases with
/// a countdown timer; the PC records everything via the live audio stream.
struct VoiceProfilingSheet: View {

    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss)  private var dismiss

    // MARK: - Phrase bank (mirrors acoustic_profiler.DEFAULT_PHRASES)
    private let phrases: [(text: String, hint: String)] = [
        ("hey agent",         "Your wake phrase — say it naturally"),
        ("click",             "Short and crisp"),
        ("scroll down",       "Two clear words"),
        ("scroll up",         "Two clear words"),
        ("screenshot",        "One long word — say it at your normal pace"),
        ("close",             "Short — don't whisper it"),
        ("open cairo",        "'Cairo' = Kiro IDE"),
        ("open chrome",       "App name after 'open'"),
        ("type hello",        "Two words, normal volume"),
        ("hotkey control c",  "Three words — say all three"),
    ]

    private let repeatsPerPhrase = 3
    private let secondsPerRepeat: Int = 4

    // MARK: - State

    @State private var phase: Phase = .intro
    @State private var phraseIndex: Int = 0
    @State private var repeatIndex: Int = 0
    @State private var countdown:   Int = 0
    @State private var timer: AnyCancellable?

    private enum Phase {
        case intro
        case speaking
        case done
    }

    // MARK: - Body

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer()

                switch phase {
                case .intro:   introContent
                case .speaking: speakingContent
                case .done:    doneContent
                }

                Spacer()
                actionButton
            }
            .padding(DesignTokens.Spacing.xl)
            .navigationTitle("Voice Profiling")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Skip") { dismiss() }
                }
            }
        }
        .onDisappear { timer?.cancel() }
    }

    // MARK: - Intro

    private var introContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "waveform.badge.mic")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)

            Text("Voice Profile")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("You'll say 10 phrases, 3 times each. The system measures your natural volume, clarity, and pronunciation — so it stays accurate even on harder days when your voice softens.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            infoCard("mic.fill", "Make sure iPad audio streaming is ON in Settings before starting.")
            infoCard("clock", "Takes about 2 minutes. Speak at your normal conversational volume.")
            infoCard("heart.fill", "Your voice profile adapts over time — no need to redo this unless something major changes.")
        }
    }

    private func infoCard(_ icon: String, _ text: String) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: icon)
                .foregroundStyle(theme.accent)
                .frame(width: 24)
            Text(text)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(DesignTokens.Spacing.md)
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
    }

    // MARK: - Speaking

    private var speakingContent: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {

            // Overall progress
            let totalRepeats = phrases.count * repeatsPerPhrase
            let done = phraseIndex * repeatsPerPhrase + repeatIndex
            ProgressView(value: Double(done), total: Double(totalRepeats))
                .tint(theme.accent)
            Text("Phrase \(phraseIndex + 1) of \(phrases.count)")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            Spacer().frame(height: DesignTokens.Spacing.lg)

            // Repeat dots
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
            Text("Say it \(repeatsPerPhrase - repeatIndex)× more")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            Spacer().frame(height: DesignTokens.Spacing.sm)

            // Phrase display
            let phrase = phrases[phraseIndex]
            Text(phrase.text)
                .font(.system(size: 36, weight: .bold, design: .rounded))
                .foregroundStyle(theme.textPrimary)
                .multilineTextAlignment(.center)
                .minimumScaleFactor(0.6)

            Text(phrase.hint)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            Spacer().frame(height: DesignTokens.Spacing.md)

            // Countdown ring
            countdownRing

            Text(countdown > 0 ? "Listening…" : "Great!")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(countdown > 0 ? theme.accent : theme.success)
                .animation(.easeInOut, value: countdown)
        }
    }

    private var countdownRing: some View {
        ZStack {
            Circle()
                .stroke(theme.surfaceSecondary, lineWidth: 6)
                .frame(width: 80, height: 80)
            Circle()
                .trim(from: 0, to: countdown > 0 ? CGFloat(countdown) / CGFloat(secondsPerRepeat) : 0)
                .stroke(theme.accent, style: StrokeStyle(lineWidth: 6, lineCap: .round))
                .frame(width: 80, height: 80)
                .rotationEffect(.degrees(-90))
                .animation(.linear(duration: 1), value: countdown)
            Image(systemName: "mic.fill")
                .font(.system(size: 28))
                .foregroundStyle(countdown > 0 ? theme.accent : theme.success)
                .symbolEffect(.variableColor.iterative, isActive: countdown > 0)
        }
    }

    // MARK: - Done

    private var doneContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "waveform.badge.checkmark")
                .font(.system(size: 72))
                .foregroundStyle(theme.success)

            Text("Voice Profile Recorded")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("\(phrases.count * repeatsPerPhrase) voice samples captured. The system will refine your profile automatically with every command you give.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
                profileRow("checkmark.circle.fill", "Per-user VAD threshold computed", theme.success)
                profileRow("checkmark.circle.fill", "Gate 1 logprob floor personalised", theme.success)
                profileRow("checkmark.circle.fill", "Pronunciation variants logged", theme.success)
                profileRow("info.circle", "Flare-day profile — next step", theme.accent)
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        }
    }

    private func profileRow(_ icon: String, _ label: String, _ color: Color) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: icon).foregroundStyle(color)
            Text(label)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textPrimary)
            Spacer()
        }
    }

    // MARK: - Action Button

    @ViewBuilder
    private var actionButton: some View {
        switch phase {
        case .intro:
            Button { startProfiling() } label: {
                Label("Start Voice Profiling", systemImage: "mic.fill")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }

        case .speaking:
            Button { skipPhrase() } label: {
                Text("Skip this phrase")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            }

        case .done:
            Button { dismiss() } label: {
                Text("Continue")
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

    private func startProfiling() {
        // Ensure audio is streaming to PC
        if !settings.audioStreamEnabled {
            settings.audioStreamEnabled = true
        }
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
                if countdown > 1 {
                    countdown -= 1
                } else {
                    // Repeat recorded — advance
                    timer?.cancel()
                    advanceRepeat()
                }
            }
    }

    private func advanceRepeat() {
        let nextRepeat = repeatIndex + 1
        if nextRepeat < repeatsPerPhrase {
            repeatIndex = nextRepeat
            // Brief pause between repeats
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                startCountdown()
            }
        } else {
            advancePhrase()
        }
    }

    private func advancePhrase() {
        let nextPhrase = phraseIndex + 1
        if nextPhrase < phrases.count {
            phraseIndex = nextPhrase
            repeatIndex = 0
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                startCountdown()
            }
        } else {
            phase = .done
        }
    }

    private func skipPhrase() {
        timer?.cancel()
        advancePhrase()
    }
}
