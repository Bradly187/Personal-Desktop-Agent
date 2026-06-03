import SwiftUI

/// Flare day profile configuration.
///
/// Guides the user through describing how their ability changes during
/// RA/JIA flare days — which sensors degrade, how much voice volume
/// drops, and whether they want to manually signal a flare day right now.
///
/// The resulting profile is stored in SettingsStore and synced to the PC
/// so BehavioralTwinState can pre-adapt thresholds before the auto-
/// detection algorithm would normally trigger.
struct FlareProfileSheet: View {

    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss)  private var dismiss

    // MARK: - Body

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: DesignTokens.Spacing.xl) {
                    headerSection
                    voiceSection
                    sensorSection
                    manualToggleSection
                    Spacer(minLength: DesignTokens.Spacing.xl)
                    saveButton
                }
                .padding(DesignTokens.Spacing.xl)
            }
            .navigationTitle("Flare Day Profile")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    // MARK: - Header

    private var headerSection: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {
            Image(systemName: "heart.text.clipboard")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)

            Text("How do flare days affect you?")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)
                .multilineTextAlignment(.center)

            Text("The system adapts thresholds based on your pattern. Being specific here makes a real difference — a voice that drops to 30% volume needs a very different silence threshold than one that drops to 70%.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - Voice section

    private var voiceSection: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.lg) {
            sectionHeader("Voice Changes", icon: "mic.fill")

            Toggle(isOn: $settings.flareVoiceDegrades) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Voice gets quieter on flare days")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textPrimary)
                    Text("Whisper and VAD thresholds will relax automatically")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            }
            .tint(theme.accent)

            if settings.flareVoiceDegrades {
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
                    Text("On a hard day, my voice is about:")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textPrimary)

                    // Volume fraction selector
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        ForEach([(0.25, "25%"), (0.40, "40%"), (0.55, "55%"), (0.70, "70%")], id: \.0) { fraction, label in
                            volumeButton(fraction: fraction, label: label)
                        }
                    }

                    Text("…of my normal volume")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)

                    Text("Current: \(Int(settings.flareVadScale * 100))% of baseline")
                        .font(DesignTokens.Typography.caption.weight(.medium))
                        .foregroundStyle(theme.accent)
                }
                .padding(DesignTokens.Spacing.lg)
                .background(theme.surfaceSecondary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .transition(.opacity.combined(with: .scale(scale: 0.97)))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: settings.flareVoiceDegrades)
    }

    private func volumeButton(fraction: Double, label: String) -> some View {
        let isSelected = abs(settings.flareVadScale - fraction) < 0.05
        return Button {
            settings.flareVadScale = fraction
        } label: {
            Text(label)
                .font(DesignTokens.Typography.body.weight(isSelected ? .semibold : .regular))
                .foregroundStyle(isSelected ? .white : theme.textPrimary)
                .frame(maxWidth: .infinity)
                .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                .background(isSelected ? theme.accent : theme.surfaceTertiary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                .animation(.easeInOut(duration: 0.15), value: isSelected)
        }
        .accessibilityLabel("Voice drops to \(label) of normal on flare days")
    }

    // MARK: - Sensor section

    private var sensorSection: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.lg) {
            sectionHeader("Other Sensors", icon: "sensor.tag.radiowaves.forward.fill")

            Text("Which other inputs get harder on flare days?")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)

            sensorToggle(
                "Hands are harder to control",
                subtitle: "Gestures get relaxed confidence thresholds",
                binding: $settings.flareGestureDegrades,
                icon: "hand.raised.fill"
            )
            sensorToggle(
                "Tilt is harder to control",
                subtitle: "Tilt sensitivity adjusts",
                binding: $settings.flareTiltDegrades,
                icon: "ipad.landscape"
            )
            sensorToggle(
                "Mouth sounds are harder to make",
                subtitle: "Sound retry cooldown shortens",
                binding: $settings.flareSoundDegrades,
                icon: "mouth.fill"
            )
        }
    }

    private func sensorToggle(
        _ title: String,
        subtitle: String,
        binding: Binding<Bool>,
        icon: String
    ) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: icon)
                .foregroundStyle(theme.accent)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textPrimary)
                Text(subtitle)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
            }
            Spacer()
            Toggle("", isOn: binding).tint(theme.accent).labelsHidden()
        }
        .padding(DesignTokens.Spacing.md)
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
    }

    // MARK: - Manual toggle

    private var manualToggleSection: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.lg) {
            sectionHeader("Right Now", icon: "calendar.badge.exclamationmark")

            HStack(spacing: DesignTokens.Spacing.md) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("I'm having a flare day today")
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(theme.textPrimary)
                    Text("Immediately adapts all thresholds — no need to wait for auto-detection")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
                Spacer()
                Toggle("", isOn: $settings.manualPainDay)
                    .tint(theme.warning)
                    .labelsHidden()
            }
            .padding(DesignTokens.Spacing.lg)
            .background(settings.manualPainDay
                        ? theme.warning.opacity(0.12)
                        : theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                    .stroke(settings.manualPainDay ? theme.warning : Color.clear, lineWidth: 1.5)
            )
            .animation(.easeInOut(duration: 0.2), value: settings.manualPainDay)

            if settings.manualPainDay {
                Label("Flare mode active — thresholds relaxed", systemImage: "checkmark.circle.fill")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.warning)
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: settings.manualPainDay)
    }

    // MARK: - Save

    private var saveButton: some View {
        Button { dismiss() } label: {
            Text("Save Profile")
                .font(DesignTokens.Typography.body.weight(.semibold))
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                .background(theme.accent)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        }
    }

    // MARK: - Helpers

    private func sectionHeader(_ title: String, icon: String) -> some View {
        HStack(spacing: DesignTokens.Spacing.sm) {
            Image(systemName: icon).foregroundStyle(theme.accent)
            Text(title)
                .font(DesignTokens.Typography.body.weight(.semibold))
                .foregroundStyle(theme.textPrimary)
        }
    }
}
