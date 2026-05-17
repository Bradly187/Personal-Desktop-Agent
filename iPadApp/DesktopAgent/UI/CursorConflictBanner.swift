import SwiftUI

/// Amber warning banner shown when 2+ cursor-movement sensors are enabled simultaneously.
/// Tapping opens a picker sheet to choose a primary cursor input.
///
/// Cursor-movement sensors: Tilt, Gaze, Head.
/// Trackpad is always available and doesn't conflict (it's direct touch, not continuous streaming).
struct CursorConflictBanner: View {
    @ObservedObject var settings: SettingsStore

    @Environment(\.appTheme) private var theme

    @State private var showPicker = false

    private var activeCursorCount: Int {
        [settings.tiltEnabled, settings.gazeEnabled, settings.headEnabled]
            .filter { $0 }
            .count
    }

    var body: some View {
        if activeCursorCount >= 2 {
            Button {
                showPicker = true
            } label: {
                HStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(theme.warning)
                    Text("Multiple cursor inputs active")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textPrimary)
                    Spacer()
                    Text("Fix")
                        .font(DesignTokens.Typography.caption.weight(.semibold))
                        .foregroundStyle(theme.accent)
                }
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.vertical, DesignTokens.Spacing.md)
                .background(theme.warning.opacity(0.15))
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Warning: multiple cursor inputs active. Tap to choose one.")
            .sheet(isPresented: $showPicker) {
                CursorPickerSheet(settings: settings)
            }
            .transition(.move(edge: .top).combined(with: .opacity))
            .animation(.easeInOut(duration: 0.3), value: activeCursorCount)
        }
    }
}

// MARK: - Picker Sheet

private struct CursorPickerSheet: View {
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Text("Choose your primary cursor input to avoid erratic movement.")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, DesignTokens.Spacing.lg)
                    .padding(.top, DesignTokens.Spacing.lg)

                VStack(spacing: DesignTokens.Spacing.md) {
                    optionRow(
                        icon: "ipad.landscape",
                        title: "Tilt",
                        subtitle: "Tilt iPad to position cursor",
                        isSelected: settings.tiltEnabled && !settings.gazeEnabled && !settings.headEnabled
                    ) {
                        settings.tiltEnabled = true
                        settings.gazeEnabled = false
                        settings.headEnabled = false
                        dismiss()
                    }

                    optionRow(
                        icon: "eye",
                        title: "Eye Gaze",
                        subtitle: "Move cursor by looking around",
                        isSelected: settings.gazeEnabled && !settings.tiltEnabled && !settings.headEnabled
                    ) {
                        settings.gazeEnabled = true
                        settings.tiltEnabled = false
                        settings.headEnabled = false
                        dismiss()
                    }

                    optionRow(
                        icon: "face.smiling",
                        title: "Head Movement",
                        subtitle: "Turn head to move cursor",
                        isSelected: settings.headEnabled && !settings.tiltEnabled && !settings.gazeEnabled
                    ) {
                        settings.headEnabled = true
                        settings.tiltEnabled = false
                        settings.gazeEnabled = false
                        dismiss()
                    }

                    optionRow(
                        icon: "hand.point.up",
                        title: "Trackpad Only",
                        subtitle: "Disable all continuous cursor sensors",
                        isSelected: !settings.tiltEnabled && !settings.gazeEnabled && !settings.headEnabled
                    ) {
                        settings.tiltEnabled = false
                        settings.gazeEnabled = false
                        settings.headEnabled = false
                        dismiss()
                    }
                }
                .padding(.horizontal, DesignTokens.Spacing.lg)

                Spacer()
            }
            .navigationTitle("Choose Cursor Input")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    private func optionRow(icon: String, title: String, subtitle: String, isSelected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: DesignTokens.Spacing.md) {
                Image(systemName: icon)
                    .font(.system(size: 22))
                    .foregroundStyle(isSelected ? theme.accent : theme.textSecondary)
                    .frame(width: 32)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(DesignTokens.Typography.body.weight(.medium))
                        .foregroundStyle(theme.textPrimary)
                    Text(subtitle)
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
                Spacer()
                if isSelected {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(theme.accent)
                }
            }
            .padding(DesignTokens.Spacing.lg)
            .background(isSelected ? theme.accent.opacity(0.1) : theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.md)
                    .stroke(isSelected ? theme.accent : Color.clear, lineWidth: 2)
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(title): \(subtitle)")
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}
