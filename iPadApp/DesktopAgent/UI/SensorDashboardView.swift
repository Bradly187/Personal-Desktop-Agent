import SwiftUI

/// Sensor Status Dashboard — shows all 6 sensors with live status,
/// quick-enable toggles, and expandable detail panels.
///
/// Each sensor card shows:
/// - Running/stopped/error state (color-coded dot)
/// - Hardware availability
/// - Quick toggle to enable/disable
/// - Tap to expand for sensor-specific live data
struct SensorDashboardView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var sensorManager: SensorManager
    @EnvironmentObject var wsManager: WebSocketManager

    @Environment(\.appTheme) private var theme

    @State private var expandedSensor: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: DesignTokens.Spacing.md) {
                    // Connection status header
                    connectionHeader
                        .padding(.horizontal, DesignTokens.Spacing.lg)

                    // Cursor control section (sensor picker, ratchet, pause)
                    CursorControlSection()
                        .padding(.horizontal, DesignTokens.Spacing.lg)

                    // Sensor cards
                    ForEach(sensorManager.sensorStates) { state in
                        SensorCard(
                            state: state,
                            isExpanded: expandedSensor == state.id,
                            onToggle: { toggleSensor(state.id) },
                            onTap: { toggleExpand(state.id) },
                            detailContent: { detailView(for: state.id) }
                        )
                        .padding(.horizontal, DesignTokens.Spacing.lg)
                    }
                }
                .padding(.vertical, DesignTokens.Spacing.lg)
            }
            .navigationTitle("Sensors")
        }
    }

    // MARK: - Connection Header

    private var connectionHeader: some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Circle()
                .fill(connectionColor)
                .frame(width: 10, height: 10)
            Text(connectionLabel)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            Spacer()
            if case .connected = wsManager.state {
                Text(String(format: "%.0f ms", wsManager.latencyMs))
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(wsManager.latencyMs < 50 ? theme.connected : theme.warning)
                    .monospacedDigit()
            }
        }
        .padding(DesignTokens.Spacing.md)
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
    }

    private var connectionColor: Color {
        switch wsManager.state {
        case .connected: return theme.connected
        case .connecting, .reconnecting: return theme.connecting
        case .disconnected: return theme.disconnected
        }
    }

    private var connectionLabel: String {
        switch wsManager.state {
        case .connected: return "Connected to PC"
        case .connecting: return "Connecting…"
        case .reconnecting(let attempt): return "Reconnecting (attempt \(attempt))…"
        case .disconnected: return "Disconnected"
        }
    }

    // MARK: - Toggle / Expand

    private func toggleSensor(_ id: String) {
        switch id {
        case "tilt": settings.tiltEnabled.toggle()
        case "gaze": settings.gazeEnabled.toggle()
        case "head": settings.headEnabled.toggle()
        case "keyword":
            if settings.keywordList.isEmpty {
                settings.keywordList = ["click", "scroll", "open"]
            } else {
                settings.keywordList = []
            }
        case "sound":
            if settings.soundMappings.isEmpty {
                settings.soundMappings = ["cluck": "CLICK", "pop": "SCROLL down", "hiss": "SCROLL up"]
            } else {
                settings.soundMappings = [:]
            }
        case "audio": settings.audioStreamEnabled.toggle()
        default: break
        }
    }

    private func toggleExpand(_ id: String) {
        withAnimation(.easeInOut(duration: 0.2)) {
            expandedSensor = expandedSensor == id ? nil : id
        }
    }

    // MARK: - Detail Views

    @ViewBuilder
    private func detailView(for id: String) -> some View {
        switch id {
        case "tilt": TiltDetailView(sensor: sensorManager.tiltSensor, settings: settings)
        case "gaze": GazeDetailView()
        case "head": HeadDetailView()
        case "keyword": KeywordDetailView(listener: sensorManager.keywordListener)
        case "sound": SoundDetailView()
        case "audio": AudioDetailView(streamer: sensorManager.audioStreamer)
        default: EmptyView()
        }
    }
}

// MARK: - Sensor Card

private struct SensorCard<Detail: View>: View {
    let state: SensorState
    let isExpanded: Bool
    let onToggle: () -> Void
    let onTap: () -> Void
    @ViewBuilder let detailContent: () -> Detail

    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(spacing: 0) {
            // Main row
            Button(action: onTap) {
                HStack(spacing: DesignTokens.Spacing.md) {
                    // Status dot
                    Circle()
                        .fill(statusColor)
                        .frame(width: 12, height: 12)

                    // Icon
                    Image(systemName: iconName)
                        .font(.system(size: 20))
                        .foregroundStyle(state.isRunning ? theme.accent : theme.textSecondary)
                        .frame(width: 28)

                    // Name + status text
                    VStack(alignment: .leading, spacing: 2) {
                        Text(displayName)
                            .font(DesignTokens.Typography.body.weight(.medium))
                            .foregroundStyle(theme.textPrimary)
                        Text(statusText)
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(statusTextColor)
                    }

                    Spacer()

                    // Toggle
                    if state.isAvailable {
                        Toggle("", isOn: Binding(
                            get: { state.isEnabled },
                            set: { _ in onToggle() }
                        ))
                        .labelsHidden()
                        .tint(theme.accent)
                    } else {
                        Text("N/A")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textSecondary)
                    }

                    // Expand chevron
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 14))
                        .foregroundStyle(theme.textSecondary)
                }
                .padding(DesignTokens.Spacing.lg)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(displayName): \(statusText)")
            .accessibilityHint("Double-tap to expand details")

            // Expanded detail
            if isExpanded {
                Divider()
                    .padding(.horizontal, DesignTokens.Spacing.lg)
                detailContent()
                    .padding(DesignTokens.Spacing.lg)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
    }

    private var statusColor: Color {
        if !state.isAvailable { return theme.textSecondary }
        if let _ = state.lastError, !state.isRunning { return theme.disconnected }
        if state.isRunning { return theme.connected }
        return theme.textSecondary.opacity(0.5)
    }

    private var statusText: String {
        if !state.isAvailable { return "Hardware not available" }
        if let error = state.lastError, !state.isRunning { return error }
        if state.isRunning { return "Running" }
        if state.isEnabled { return "Enabled, not started" }
        return "Disabled"
    }

    private var statusTextColor: Color {
        if !state.isAvailable { return theme.textSecondary }
        if state.lastError != nil && !state.isRunning { return theme.disconnected }
        if state.isRunning { return theme.connected }
        return theme.textSecondary
    }

    private var displayName: String {
        switch state.id {
        case "tilt": return "Tilt Navigation"
        case "gaze": return "Eye Gaze"
        case "head": return "Head Tracking"
        case "keyword": return "Voice Keywords"
        case "sound": return "Sound Actions"
        case "audio": return "Audio Stream"
        default: return state.id.capitalized
        }
    }

    private var iconName: String {
        switch state.id {
        case "tilt": return "ipad.landscape"
        case "gaze": return "eye"
        case "head": return "face.smiling"
        case "keyword": return "text.bubble"
        case "sound": return "mouth"
        case "audio": return "mic.badge.plus"
        default: return "questionmark"
        }
    }
}

// MARK: - Detail Views

private struct TiltDetailView: View {
    @ObservedObject var sensor: TiltSensor
    @ObservedObject var settings: SettingsStore
    @EnvironmentObject var sensorManager: SensorManager
    @Environment(\.appTheme) private var theme

    @State private var showCalibration = false

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Mode: \(settings.tiltPositionMode ? "Position" : "Velocity")")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    Text("Range: \(Int(settings.tiltRange))°  Dead Zone: \(String(format: "%.2f", settings.tiltDeadZone))")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    Text(String(format: "Last position: (%.3f, %.3f)", sensor.lastSentX, sensor.lastSentY))
                        .font(DesignTokens.Typography.mono)
                        .foregroundStyle(theme.textPrimary)
                }
                Spacer()
                // Mini position indicator
                tiltIndicator
            }

            Button("Calibrate Neutral Position…") {
                showCalibration = true
            }
            .font(DesignTokens.Typography.caption)
            .foregroundStyle(theme.accent)
            .sheet(isPresented: $showCalibration) {
                TiltCalibrationSheet(sensorManager: sensorManager)
            }
        }
    }

    private var tiltIndicator: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 4)
                .stroke(theme.textSecondary.opacity(0.3), lineWidth: 1)
                .frame(width: 60, height: 60)
            Circle()
                .fill(theme.accent)
                .frame(width: 8, height: 8)
                .offset(
                    x: CGFloat(sensor.lastSentX - 0.5) * 50,
                    y: CGFloat(sensor.lastSentY - 0.5) * 50
                )
        }
        .accessibilityLabel(String(format: "Tilt position: %.0f%% right, %.0f%% down", sensor.lastSentX * 100, sensor.lastSentY * 100))
    }
}

private struct GazeDetailView: View {
    @EnvironmentObject var sensorManager: SensorManager
    @EnvironmentObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme

    @State private var showCalibration = false

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            Text("Sends relative eye movement deltas to PC.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            Text("Blink detection active — skips frames when eyes closed.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            HStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: "eyeglasses")
                    .foregroundStyle(theme.accent)
                Text("Increase Smoothing in Settings if using glasses.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
            }
            Text("Sensitivity: \(Int(settings.gazeSensitivity))  Smoothing: \(String(format: "%.2f", settings.gazeStabilityThreshold))")
                .font(DesignTokens.Typography.mono)
                .foregroundStyle(theme.textPrimary)
                .padding(.top, DesignTokens.Spacing.xs)

            Button("Auto-Tune Sensitivity…") {
                showCalibration = true
            }
            .font(DesignTokens.Typography.caption)
            .foregroundStyle(theme.accent)
            .padding(.top, DesignTokens.Spacing.xs)
            .sheet(isPresented: $showCalibration) {
                GazeCalibrationSheet(sensorManager: sensorManager, settings: settings)
            }
        }
    }
}

private struct HeadDetailView: View {
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            Text("Sends head pitch/yaw deltas to PC for cursor movement.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            Text("Shares TrueDepth camera with Eye Gaze (no conflict).")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
        }
    }
}

private struct KeywordDetailView: View {
    @ObservedObject var listener: KeywordListener
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            HStack {
                Text("Status:")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                Text(listener.isListening ? "Listening" : "Idle")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(listener.isListening ? theme.connected : theme.textSecondary)
            }
            if let last = listener.lastKeyword {
                HStack(spacing: DesignTokens.Spacing.sm) {
                    Text("Last recognized:")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    Text("\"\(last)\"")
                        .font(DesignTokens.Typography.mono)
                        .foregroundStyle(theme.accent)
                }
            }
            Text("On-device Speech Framework (en-US). Restarts automatically on timeout.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
        }
    }
}

private struct SoundDetailView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme

    @State private var showTraining = false

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            Text("Detects mouth sounds via FFT spectral analysis.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            Text("Supported: cluck (mid-freq burst), pop (broad burst), hiss (high-freq sustained)")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            Text("200ms debounce between detections.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            Button("Test Your Sounds…") {
                showTraining = true
            }
            .font(DesignTokens.Typography.caption)
            .foregroundStyle(theme.accent)
            .padding(.top, DesignTokens.Spacing.xs)
            .sheet(isPresented: $showTraining) {
                SoundTrainingSheet(wsManager: wsManager, settings: settings)
            }
        }
    }
}

private struct AudioDetailView: View {
    @ObservedObject var streamer: AudioStreamer
    @EnvironmentObject var wsManager: WebSocketManager
    @Environment(\.appTheme) private var theme

    @State private var showVoiceCalibration = false

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            HStack {
                Text("Status:")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                Text(streamer.isStreaming ? "Streaming" : "Idle")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(streamer.isStreaming ? theme.connected : theme.textSecondary)
            }
            Text("16 kHz mono PCM → PC Whisper large-v3 for full voice commands.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
            Text("50ms chunks, base64 over WebSocket.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            // Voice calibration entry point
            Button("Calibrate My Voice…") {
                showVoiceCalibration = true
            }
            .font(DesignTokens.Typography.caption)
            .foregroundStyle(theme.accent)
            .padding(.top, DesignTokens.Spacing.xs)
            .accessibilityLabel("Open voice calibration — teach the system how your voice sounds today")
            .sheet(isPresented: $showVoiceCalibration) {
                VoiceCalibrationSheet(wsManager: wsManager)
            }
        }
    }
}

// MARK: - Cursor Control Section

/// Top-level cursor control panel showing:
/// - Active cursor sensor picker (segmented: tilt / gaze / head)
/// - Ratchet button (re-center tilt without cursor jump)
/// - Pause indicator (shows when cursor sensors are paused)
///
/// Design constraints:
/// - Large touch targets (80pt min) for JIA accessibility
/// - No startling transitions (SVT-safe)
/// - Clear, predictable state indicators (bipolar-friendly)
private struct CursorControlSection: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var sensorManager: SensorManager
    @EnvironmentObject var wsManager: WebSocketManager
    @Environment(\.appTheme) private var theme

    @State private var isPaused = false
    @State private var showRatchetConfirmation = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.md) {
            // Section header
            HStack {
                Image(systemName: "cursorarrow.motionlines")
                    .foregroundStyle(theme.accent)
                Text("Cursor Control")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(theme.textPrimary)
                Spacer()
                // Pause indicator
                if isPaused {
                    HStack(spacing: 4) {
                        Image(systemName: "pause.circle.fill")
                            .foregroundStyle(theme.warning)
                        Text("Paused")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.warning)
                    }
                }
            }

            // Cursor sensor picker (segmented control)
            cursorSensorPicker

            // Action buttons row
            HStack(spacing: DesignTokens.Spacing.md) {
                // Ratchet button (large touch target)
                ratchetButton

                // Pause/Resume button
                pauseButton
            }
        }
        .padding(DesignTokens.Spacing.lg)
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
    }

    // MARK: - Cursor Sensor Picker

    private var cursorSensorPicker: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            Text("Active Cursor Sensor")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            HStack(spacing: 0) {
                sensorPickerButton(label: "Tilt", icon: "ipad.landscape", sensor: "tilt")
                sensorPickerButton(label: "Gaze", icon: "eye", sensor: "gaze")
                sensorPickerButton(label: "Head", icon: "face.smiling", sensor: "head")
            }
            .background(theme.surfacePrimary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
        }
    }

    private func sensorPickerButton(label: String, icon: String, sensor: String) -> some View {
        let isActive = settings.activeCursorSensor == sensor
        return Button {
            selectCursorSensor(sensor)
        } label: {
            VStack(spacing: 4) {
                Image(systemName: icon)
                    .font(.system(size: 16))
                Text(label)
                    .font(DesignTokens.Typography.caption)
            }
            .frame(maxWidth: .infinity)
            .frame(minHeight: 56)
            .foregroundStyle(isActive ? theme.accent : theme.textSecondary)
            .background(isActive ? theme.accent.opacity(0.15) : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(label) cursor sensor")
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }

    // MARK: - Ratchet Button

    private var ratchetButton: some View {
        Button {
            triggerRatchet()
        } label: {
            VStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: showRatchetConfirmation ? "checkmark.circle" : "arrow.counterclockwise")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(showRatchetConfirmation ? theme.connected : theme.accent)
                Text("Re-center")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
            }
            .frame(minWidth: DesignTokens.Size.touchTargetMin,
                   minHeight: DesignTokens.Size.touchTargetMin)
            .background(theme.surfacePrimary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Re-center tilt")
        .accessibilityHint("Resets tilt neutral point without moving cursor")
        .disabled(settings.activeCursorSensor != "tilt")
        .opacity(settings.activeCursorSensor == "tilt" ? 1.0 : 0.4)
    }

    // MARK: - Pause Button

    private var pauseButton: some View {
        Button {
            togglePause()
        } label: {
            VStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: isPaused ? "play.circle" : "pause.circle")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(isPaused ? theme.connected : theme.accent)
                Text(isPaused ? "Resume" : "Pause")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
            }
            .frame(minWidth: DesignTokens.Size.touchTargetMin,
                   minHeight: DesignTokens.Size.touchTargetMin)
            .background(isPaused ? theme.warning.opacity(0.1) : theme.surfacePrimary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(isPaused ? "Resume cursor" : "Pause cursor")
        .accessibilityHint(isPaused ? "Resumes cursor movement from current position" : "Freezes cursor in place")
    }

    // MARK: - Actions

    private func selectCursorSensor(_ sensor: String) {
        guard sensor != settings.activeCursorSensor else { return }

        let fromSensor = settings.activeCursorSensor
        settings.activeCursorSensor = sensor

        // Disable old cursor sensor, enable new one
        switch fromSensor {
        case "tilt": settings.tiltEnabled = false
        case "gaze": settings.gazeEnabled = false
        case "head": settings.headEnabled = false
        default: break
        }
        switch sensor {
        case "tilt": settings.tiltEnabled = true
        case "gaze": settings.gazeEnabled = true
        case "head": settings.headEnabled = true
        default: break
        }

        // Notify PC for 200ms cursor hold
        wsManager.sendSensorSwitch(from: fromSensor, to: sensor)
    }

    private func triggerRatchet() {
        sensorManager.tiltSensor.ratchet()

        // Brief visual confirmation (200-400ms, no sound — SVT-safe)
        showRatchetConfirmation = true
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)  // 300ms
            showRatchetConfirmation = false
        }
    }

    private func togglePause() {
        isPaused.toggle()
        if isPaused {
            wsManager.sendCursorPause()
        } else {
            wsManager.sendCursorResume()
        }
        // Light haptic — subtle confirmation (SVT-safe)
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
    }
}
