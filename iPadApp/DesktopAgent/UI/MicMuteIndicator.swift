import SwiftUI

/// Persistent, always-visible microphone mute indicator and toggle.
///
/// Safety-critical: voice "mute mic" is one-way (the mic goes deaf, so no voice
/// command — including "unmute" — can be heard afterward). This pill is the
/// primary way to UNmute. It mirrors `WebSocketManager.micMuted`, which the PC
/// keeps in sync via the `mic_state` push for both voice- and iPad-initiated
/// mutes. Tapping toggles the state.
struct MicMuteIndicator: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @Environment(\.appTheme) private var theme

    var body: some View {
        let muted = wsManager.micMuted
        Button {
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            wsManager.sendMicMute(!muted)
        } label: {
            HStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: muted ? "mic.slash.fill" : "mic.fill")
                    .font(.system(size: 16, weight: .semibold))
                Text(muted ? "Muted" : "Mic on")
                    .font(DesignTokens.Typography.caption.weight(.semibold))
            }
            .foregroundStyle(muted ? Color.white : theme.accent)
            .padding(.horizontal, DesignTokens.Spacing.md)
            .frame(minHeight: DesignTokens.Size.touchTargetMin)
            .background(
                Capsule()
                    .fill(muted ? theme.destructive : theme.accent.opacity(0.12))
            )
            .overlay(
                Capsule()
                    .strokeBorder(muted ? theme.destructive : theme.accent.opacity(0.6),
                                  lineWidth: 2)
            )
            .shadow(color: .black.opacity(muted ? 0.2 : 0.08),
                    radius: muted ? 5 : 2, x: 0, y: 1)
        }
        .buttonStyle(.plain)
        .animation(.easeInOut(duration: 0.18), value: muted)
        .accessibilityLabel(muted ? "Microphone muted" : "Microphone on")
        .accessibilityHint(muted ? "Double tap to unmute" : "Double tap to mute")
        .accessibilityAddTraits(.isButton)
    }
}
