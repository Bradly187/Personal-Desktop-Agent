import SwiftUI
import Combine

/// Guided sound training flow that validates the user can produce
/// sounds the detector recognizes (cluck, pop, hiss).
///
/// For each sound:
/// - Shows instruction + animated mic indicator
/// - Listens for 3 successful detections via WebSocketManager.commandFeed
/// - Shows progress (0/3 → 3/3) per sound
/// - If 5 attempts fail: suggests adjustments
///
/// Does NOT retrain FFT thresholds — validates user compatibility.
struct SoundTrainingSheet: View {
    @ObservedObject var wsManager: WebSocketManager
    @ObservedObject var settings: SettingsStore

    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss) private var dismiss

    @State private var currentSoundIndex = 0
    @State private var detectionCounts: [String: Int] = ["cluck": 0, "pop": 0, "hiss": 0]
    @State private var attemptCounts: [String: Int] = ["cluck": 0, "pop": 0, "hiss": 0]
    @State private var phase: TrainingPhase = .intro
    @State private var cancellable: AnyCancellable?

    private let sounds = ["cluck", "pop", "hiss"]
    private let requiredDetections = 3
    private let maxAttempts = 8

    private enum TrainingPhase {
        case intro
        case training
        case done
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer()

                switch phase {
                case .intro:
                    introContent
                case .training:
                    trainingContent
                case .done:
                    doneContent
                }

                Spacer()

                // Action button
                actionButton
            }
            .padding(DesignTokens.Spacing.xl)
            .navigationTitle("Sound Training")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        cancellable?.cancel()
                        dismiss()
                    }
                }
            }
        }
        .onDisappear {
            cancellable?.cancel()
        }
    }

    // MARK: - Intro

    private var introContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "mouth")
                .font(.system(size: 64))
                .foregroundStyle(theme.accent)

            Text("Train Sound Actions")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("We'll test that your device can detect three mouth sounds. Make each sound 3 times when prompted.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                soundDescription("cluck", "A sharp tongue click (like calling a horse)")
                soundDescription("pop", "A lip pop (like a kiss sound)")
                soundDescription("hiss", "A sustained 'sss' or 'shh' sound")
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        }
    }

    private func soundDescription(_ name: String, _ desc: String) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Text(name)
                .font(DesignTokens.Typography.body.weight(.semibold))
                .foregroundStyle(theme.accent)
                .frame(width: 50, alignment: .leading)
            Text(desc)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
        }
    }

    // MARK: - Training

    private var trainingContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            // Progress dots for all sounds
            HStack(spacing: DesignTokens.Spacing.lg) {
                ForEach(sounds, id: \.self) { sound in
                    soundProgressDot(sound)
                }
            }

            let currentSound = sounds[currentSoundIndex]
            let count = detectionCounts[currentSound] ?? 0
            let attempts = attemptCounts[currentSound] ?? 0

            // Current sound instruction
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)
                .symbolEffect(.variableColor.iterative, isActive: true)

            Text("Make a \"\(currentSound)\" sound")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            // Progress
            HStack(spacing: DesignTokens.Spacing.sm) {
                ForEach(0..<requiredDetections, id: \.self) { i in
                    Circle()
                        .fill(i < count ? theme.success : theme.surfaceTertiary)
                        .frame(width: 16, height: 16)
                        .animation(.easeInOut(duration: 0.2), value: count)
                }
            }

            Text("\(count)/\(requiredDetections) detected")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            if attempts >= 5 && count < requiredDetections {
                Text("Having trouble? Try making the sound sharper and closer to the iPad mic.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.warning)
                    .multilineTextAlignment(.center)
            }
        }
    }

    private func soundProgressDot(_ sound: String) -> some View {
        let count = detectionCounts[sound] ?? 0
        let isCurrent = sounds[currentSoundIndex] == sound
        let isComplete = count >= requiredDetections

        return VStack(spacing: 4) {
            Circle()
                .fill(isComplete ? theme.success : isCurrent ? theme.accent : theme.surfaceTertiary)
                .frame(width: 12, height: 12)
            Text(sound)
                .font(.system(size: 10))
                .foregroundStyle(isCurrent ? theme.textPrimary : theme.textSecondary)
        }
    }

    // MARK: - Done

    private var doneContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.success)

            Text("Sound Training Complete!")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                ForEach(sounds, id: \.self) { sound in
                    let count = detectionCounts[sound] ?? 0
                    HStack {
                        Image(systemName: count >= requiredDetections ? "checkmark.circle.fill" : "xmark.circle")
                            .foregroundStyle(count >= requiredDetections ? theme.success : theme.warning)
                        Text(sound)
                            .font(DesignTokens.Typography.body)
                            .foregroundStyle(theme.textPrimary)
                        Spacer()
                        Text("\(count)/\(requiredDetections)")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textSecondary)
                    }
                }
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))

            Text("Sounds that weren't detected may not work well in your environment. You can adjust mappings in Settings.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - Action Button

    @ViewBuilder
    private var actionButton: some View {
        switch phase {
        case .intro:
            Button {
                startTraining()
            } label: {
                Text("Start Training")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }

        case .training:
            Button {
                skipCurrentSound()
            } label: {
                Text("Skip this sound")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            }

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
        }
    }

    // MARK: - Logic

    private func startTraining() {
        phase = .training
        currentSoundIndex = 0

        // Ensure sound mappings are active so SoundDetector fires
        if settings.soundMappings.isEmpty {
            settings.soundMappings = ["cluck": "CLICK", "pop": "SCROLL down", "hiss": "SCROLL up"]
        }

        // Listen for sound detections via commandFeed
        cancellable = wsManager.commandFeed
            .filter { $0.hasPrefix("Sound: ") }
            .receive(on: DispatchQueue.main)
            .sink { message in
                let detected = String(message.dropFirst("Sound: ".count))
                handleDetection(detected)
            }
    }

    private func handleDetection(_ sound: String) {
        guard phase == .training else { return }
        let currentSound = sounds[currentSoundIndex]

        // Count attempts for current sound (any detection counts as an attempt)
        attemptCounts[currentSound, default: 0] += 1

        // Only count if it matches the expected sound
        if sound == currentSound {
            detectionCounts[currentSound, default: 0] += 1
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()

            if (detectionCounts[currentSound] ?? 0) >= requiredDetections {
                advanceToNextSound()
            }
        }
    }

    private func skipCurrentSound() {
        advanceToNextSound()
    }

    private func advanceToNextSound() {
        if currentSoundIndex < sounds.count - 1 {
            currentSoundIndex += 1
        } else {
            phase = .done
            cancellable?.cancel()
        }
    }
}
