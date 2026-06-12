import SwiftUI

/// Guided voice calibration flow.
///
/// Sends a `calibration_start` WebSocket message to the PC with the chosen
/// condition.  The PC's VoiceCalibrator drives the session — it plays each
/// TTS prompt, listens for the user's response, and sends back progress and
/// completion events.  This view reacts to those incoming events.
///
/// Conditions:
///   good_day    — baseline (feeling well)
///   flare_day   — RA flare
///   allergy_day — nasal congestion
struct VoiceCalibrationSheet: View {
    @ObservedObject var wsManager: WebSocketManager
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss) private var dismiss

    @State private var selectedCondition: VoiceCondition = .goodDay
    @State private var quick: Bool = false
    @State private var phase: Phase = .conditionPicker
    @State private var currentPhrase: String = ""
    @State private var phraseIndex: Int = 0
    @State private var totalPhrases: Int = 0
    @State private var lastResult: PhraseOutcome? = nil
    @State private var report: CalibrationReport? = nil

    // MARK: — Types

    enum VoiceCondition: String, CaseIterable {
        case goodDay    = "good_day"
        case flareDay   = "flare_day"
        case allergyDay = "allergy_day"

        var display: String {
            switch self {
            case .goodDay:    return "Good day"
            case .flareDay:   return "Flare day"
            case .allergyDay: return "Allergy / congestion"
            }
        }

        var icon: String {
            switch self {
            case .goodDay:    return "sun.max.fill"
            case .flareDay:   return "flame.fill"
            case .allergyDay: return "allergens"
            }
        }

        var description: String {
            switch self {
            case .goodDay:
                return "Baseline calibration. Speak at your normal volume and pace."
            case .flareDay:
                return "RA flare active. Speak at whatever volume is comfortable — the system will adapt."
            case .allergyDay:
                return "Congested voice. Speak as clearly as you can given the congestion."
            }
        }
    }

    enum Phase {
        case conditionPicker
        case running
        case done
        case cancelled
    }

    struct PhraseOutcome {
        let expected: String
        let heard: String
        let matched: Bool
    }

    struct CalibrationReport {
        let accuracy: Double
        let correctionsAdded: Int
    }

    // MARK: — Body

    var body: some View {
        NavigationStack {
            Group {
                switch phase {
                case .conditionPicker: conditionPickerView
                case .running:         runningView
                case .done:            doneView
                case .cancelled:       cancelledView
                }
            }
            .navigationTitle("Voice Calibration")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        if phase == .running {
                            wsManager.sendCalibrationCancel()
                        }
                        phase = .cancelled
                        dismiss()
                    }
                }
            }
        }
        .onReceive(wsManager.calibrationEventPublisher) { event in
            handleCalibrationEvent(event)
        }
    }

    // MARK: — Condition picker

    private var conditionPickerView: some View {
        ScrollView {
            VStack(spacing: DesignTokens.Spacing.lg) {
                // Header
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: "waveform.and.mic")
                        .font(.system(size: 52))
                        .foregroundStyle(theme.accent)
                        .padding(.top, DesignTokens.Spacing.xl)

                    Text("Voice Calibration")
                        .font(DesignTokens.Typography.headline)
                        .foregroundStyle(theme.textPrimary)

                    Text("Teaches the system how you sound today.\nRun a new session whenever your voice changes.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                        .multilineTextAlignment(.center)
                }
                .padding(.horizontal)

                // Condition picker
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Text("How are you feeling today?")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textPrimary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal)

                    ForEach(VoiceCondition.allCases, id: \.self) { condition in
                        conditionCard(condition)
                    }
                }

                // Quick vs full toggle
                VStack(spacing: DesignTokens.Spacing.xs) {
                    Toggle(isOn: $quick) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Quick calibration (5 phrases, ~45s)")
                                .font(DesignTokens.Typography.body)
                                .foregroundStyle(theme.textPrimary)
                            Text("Full session: 20 phrases, ~3 minutes")
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(theme.textSecondary)
                        }
                    }
                    .padding()
                    .background(theme.surfaceSecondary)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    .padding(.horizontal)
                }

                // Start button
                Button {
                    startCalibration()
                } label: {
                    HStack {
                        Image(systemName: "mic.fill")
                        Text("Start Calibration")
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(theme.accent)
                    .foregroundStyle(.white)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }
                .padding(.horizontal)
                .padding(.bottom, DesignTokens.Spacing.xl)
                .accessibilityLabel("Start voice calibration for \(selectedCondition.display)")
            }
        }
        .background(theme.surfacePrimary)
    }

    private func conditionCard(_ condition: VoiceCondition) -> some View {
        Button {
            selectedCondition = condition
        } label: {
            HStack(spacing: DesignTokens.Spacing.md) {
                Image(systemName: condition.icon)
                    .font(.title2)
                    .foregroundStyle(selectedCondition == condition ? .white : theme.accent)
                    .frame(width: 36)

                VStack(alignment: .leading, spacing: 4) {
                    Text(condition.display)
                        .font(DesignTokens.Typography.body)
                        .fontWeight(selectedCondition == condition ? .semibold : .regular)
                        .foregroundStyle(selectedCondition == condition ? .white : theme.textPrimary)
                    Text(condition.description)
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(selectedCondition == condition ? .white.opacity(0.8) : theme.textSecondary)
                        .multilineTextAlignment(.leading)
                }

                Spacer()

                if selectedCondition == condition {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.white)
                }
            }
            .padding()
            .background(selectedCondition == condition ? theme.accent : theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .padding(.horizontal)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selectedCondition == condition ? .isSelected : [])
    }

    // MARK: — Running view

    private var runningView: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            // Progress
            if totalPhrases > 0 {
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Text("\(phraseIndex) of \(totalPhrases)")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    ProgressView(value: Double(phraseIndex), total: Double(totalPhrases))
                        .tint(theme.accent)
                        .padding(.horizontal)
                }
            }

            // Current phrase
            VStack(spacing: DesignTokens.Spacing.md) {
                Text("Say this phrase:")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)

                Text(currentPhrase.isEmpty ? "Waiting…" : currentPhrase)
                    .font(.system(size: 28, weight: .bold, design: .rounded))
                    .foregroundStyle(theme.textPrimary)
                    .multilineTextAlignment(.center)
                    .padding()
                    .frame(maxWidth: .infinity)
                    .background(theme.surfaceSecondary)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.lg))
                    .padding(.horizontal)
            }

            // Last result feedback
            if let result = lastResult {
                HStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: result.matched ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(result.matched ? theme.connected : theme.warning)
                    VStack(alignment: .leading) {
                        Text(result.matched ? "Matched!" : "Heard: \"\(result.heard)\"")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textPrimary)
                        if !result.matched {
                            Text("Correction saved automatically")
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(theme.textSecondary)
                        }
                    }
                }
                .padding()
                .background(theme.surfaceSecondary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .padding(.horizontal)
                .transition(.opacity.combined(with: .scale))
            }

            // Mic indicator
            HStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: "mic.fill")
                    .foregroundStyle(theme.accent)
                    .symbolEffect(.variableColor.iterative)
                Text("Listening…")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
            }

            Spacer()
        }
        .background(theme.surfacePrimary)
        .animation(.easeInOut(duration: 0.3), value: lastResult?.expected)
    }

    // MARK: — Done view

    private var doneView: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.connected)

            Text("Calibration Complete!")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            if let r = report {
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Text(String(format: "%.0f%% accuracy", r.accuracy * 100))
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textPrimary)
                    Text("\(r.correctionsAdded) personal corrections saved")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    Text("Profile: \(selectedCondition.display)")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            }

            Text("The system has adapted to your voice for this condition.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal)

            Button("Done") { dismiss() }
                .buttonStyle(.borderedProminent)
                .tint(theme.accent)

            Spacer()
        }
        .background(theme.surfacePrimary)
    }

    // MARK: — Cancelled view

    private var cancelledView: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {
            Spacer()
            Image(systemName: "xmark.circle")
                .font(.system(size: 64))
                .foregroundStyle(theme.textSecondary)
            Text("Calibration cancelled")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)
            Spacer()
        }
        .background(theme.surfacePrimary)
    }

    // MARK: — Actions

    private func startCalibration() {
        phase = .running
        phraseIndex = 0
        lastResult = nil
        wsManager.sendCalibrationStart(
            condition: selectedCondition.rawValue,
            quick: quick
        )
    }

    private func handleCalibrationEvent(_ event: CalibrationEvent) {
        switch event {
        case .phrasePrompt(let phrase, let index, let total):
            currentPhrase = phrase
            phraseIndex = index
            totalPhrases = total

        case .phraseResult(let expected, let heard, let matched):
            withAnimation {
                lastResult = PhraseOutcome(expected: expected, heard: heard, matched: matched)
            }

        case .complete(let accuracy, let corrections):
            report = CalibrationReport(accuracy: accuracy, correctionsAdded: corrections)
            withAnimation { phase = .done }

        case .error(let msg):
            AppLogger.shared.warning("VoiceCalibrationSheet", "Calibration error: \(msg)")
            phase = .cancelled
        }
    }
}

// MARK: — CalibrationEvent (matches PC WebSocket protocol)

enum CalibrationEvent {
    case phrasePrompt(phrase: String, index: Int, total: Int)
    case phraseResult(expected: String, heard: String, matched: Bool)
    case complete(accuracy: Double, correctionsAdded: Int)
    case error(String)
}
