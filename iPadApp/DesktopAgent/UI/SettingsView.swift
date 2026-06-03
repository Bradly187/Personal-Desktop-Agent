import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var serviceDiscovery: ServiceDiscovery
    @EnvironmentObject var sensorManager: SensorManager

    @Environment(\.appTheme) private var theme

    @State private var newSoundName = ""
    @State private var newSoundAction = ""
    @State private var newKeyword = ""
    // G1: voice calibration sheets
    @State private var showVoiceProfilingSheet = false
    @State private var showVoiceCalibrationSheet = false
    // G7: command button export
    @State private var showExportShareSheet = false
    @State private var exportURL: URL? = nil
    // Re-run onboarding confirmation
    @State private var showRerunOnboardingAlert = false
    @AppStorage("onboardingComplete") private var onboardingComplete = false
    @AppStorage("onboardingCurrentStep") private var onboardingCurrentStep = 0

    var body: some View {
        NavigationStack {
            Form {
                // Connection
                Section {
                    LabeledContent("Host") {
                        TextField("192.168.1.100", text: $settings.serverHost)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.URL)
                            .autocorrectionDisabled()
                    }
                    LabeledContent("Port") {
                        TextField("8765", text: Binding(
                            get: { String(settings.serverPort) },
                            set: { settings.serverPort = Int($0.filter(\.isWholeNumber)) ?? 8765 }
                        ))
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numberPad)
                    }

                    if settings.wsURL == nil {
                        Label("Invalid host or port. Using default address.", systemImage: "exclamationmark.triangle.fill")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(.orange)
                            .accessibilityLabel("Warning: Invalid host or port. The app will use the default address ws://192.168.1.100:8765/ws.")
                    }

                    // mDNS Discovery Status
                    if let host = serviceDiscovery.discoveredHost,
                       let port = serviceDiscovery.discoveredPort {
                        HStack(spacing: DesignTokens.Spacing.sm) {
                            Image(systemName: "bonjour")
                                .foregroundStyle(theme.connected)
                            VStack(alignment: .leading, spacing: DesignTokens.Spacing.xs) {
                                Text("Discovered via mDNS")
                                    .font(DesignTokens.Typography.caption)
                                    .foregroundStyle(theme.textSecondary)
                                Text("\(host):\(port)")
                                    .font(DesignTokens.Typography.caption)
                                    .foregroundStyle(theme.textPrimary)
                            }
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("Service discovered via mDNS at \(host) port \(port)")
                    } else if serviceDiscovery.isSearching {
                        HStack(spacing: DesignTokens.Spacing.sm) {
                            ProgressView()
                                .controlSize(.small)
                            Text("Searching for desktop agent…")
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(theme.textSecondary)
                        }
                        .accessibilityLabel("Searching for desktop agent service on local network")
                    }

                    Button("Reconnect") { wsManager.disconnect(); wsManager.connect() }
                        .accessibilityHint("Double-tap to disconnect and reconnect to the PC")

                    // Latency monitor
                    if case .connected = wsManager.state {
                        LabeledContent("Latency") {
                            Text(String(format: "%.1f ms", wsManager.latencyMs))
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(wsManager.latencyMs < 10 ? theme.connected : wsManager.latencyMs < 50 ? theme.connecting : theme.disconnected)
                        }
                    }
                } header: {
                    DASectionHeader(title: "Connection")
                }

                // Tilt
                Section {
                    Toggle("Enable Tilt", isOn: $settings.tiltEnabled)
                    Toggle("Position Mode", isOn: $settings.tiltPositionMode)
                    Text("Maps tilt angle to absolute screen position. When off, uses legacy velocity mode.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    Toggle("Invert Tilt", isOn: $settings.tiltInverted)
                    LabeledContent("Tilt Range: \(Int(settings.tiltRange))°") {
                        Slider(value: $settings.tiltRange, in: 5...60, step: 1)
                    }
                    LabeledContent("Sensitivity") {
                        Slider(value: $settings.tiltSensitivity, in: 0.1...5.0)
                    }
                    LabeledContent("Dead Zone: \(String(format: "%.1f°", settings.tiltDeadZone))") {
                        Slider(value: $settings.tiltDeadZone, in: 0.5...5.0)
                    }
                    Toggle("Dwell Click", isOn: $settings.tiltDwellClickEnabled)
                    Text("Hold the cursor still to click. Fires the action selected in the dwell toolbar.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                    if settings.tiltDwellClickEnabled {
                        LabeledContent("Dwell Time: \(String(format: "%.1fs", settings.tiltDwellDuration))") {
                            Slider(value: $settings.tiltDwellDuration, in: 0.5...2.5, step: 0.1)
                        }
                    }
                    Button("Calibrate Neutral") {
                        sensorManager.tiltSensor.calibrate()
                    }
                    .disabled(!settings.tiltEnabled)
                    .accessibilityHint("Double-tap to set the current iPad orientation as the neutral center position")
                } header: {
                    DASectionHeader(title: "Tilt Navigation")
                }

                // Trackpad
                Section {
                    LabeledContent("Speed") {
                        Slider(value: $settings.trackpadSpeed, in: 0.5...5.0)
                    }
                    LabeledContent("Palm Reject Radius") {
                        Slider(value: $settings.palmRejectRadius, in: 10...60)
                    }
                } header: {
                    DASectionHeader(title: "Trackpad")
                }

                // Keywords
                Section {
                    ForEach(settings.keywordList, id: \.self) { keyword in
                        Text(keyword)
                            .foregroundStyle(theme.textPrimary)
                    }
                    .onDelete { idx in settings.keywordList.remove(atOffsets: idx) }
                    HStack {
                        TextField("New keyword", text: $newKeyword)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .onSubmit { _addKeyword() }
                        Button("Add", action: _addKeyword)
                            .disabled(newKeyword.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                } header: {
                    DASectionHeader(title: "Voice Keywords")
                }

                // Audio Streaming
                Section {
                    Toggle("Stream mic to PC (Whisper)", isOn: $settings.audioStreamEnabled)
                    Text("Sends iPad microphone audio to the PC for full voice command processing via Whisper large-v3.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                } header: {
                    DASectionHeader(title: "Audio Streaming")
                }

                // Sound mappings
                Section {
                    ForEach(settings.soundMappings.keys.sorted(), id: \.self) { sound in
                        HStack {
                            Text(sound)
                                .foregroundStyle(theme.textPrimary)
                            Spacer()
                            TextField("Action", text: Binding(
                                get: { settings.soundMappings[sound] ?? "" },
                                set: { settings.soundMappings[sound] = $0 }
                            ))
                            .multilineTextAlignment(.trailing)
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                            .foregroundStyle(theme.textSecondary)
                            .frame(width: 140)
                        }
                    }
                    .onDelete { idx in
                        let keys = settings.soundMappings.keys.sorted()
                        idx.forEach { settings.soundMappings.removeValue(forKey: keys[$0]) }
                    }
                    HStack {
                        TextField("Sound", text: $newSoundName)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                        Spacer()
                        TextField("Action", text: $newSoundAction)
                            .multilineTextAlignment(.trailing)
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                            .frame(width: 140)
                    }
                    .foregroundStyle(theme.textSecondary)
                    Button("Add mapping…") {
                        let key = newSoundName.trimmingCharacters(in: .whitespaces)
                        let val = newSoundAction.trimmingCharacters(in: .whitespaces)
                        guard !key.isEmpty, !val.isEmpty else { return }
                        settings.soundMappings[key] = val
                        newSoundName = ""
                        newSoundAction = ""
                    }
                    .disabled(newSoundName.trimmingCharacters(in: .whitespaces).isEmpty ||
                              newSoundAction.trimmingCharacters(in: .whitespaces).isEmpty)
                } header: {
                    DASectionHeader(title: "Sound Mappings")
                }

                // G1: Voice Calibration — G11: distinguish the two flows clearly
                Section {
                    // Acoustic Profiling: local measurement, no PC round-trip
                    Button("Acoustic Profile (offline)") {
                        showVoiceProfilingSheet = true
                    }
                    .accessibilityHint("Measures your voice volume and clarity using the iPad mic. No PC connection needed.")

                    Text("Records baseline RMS and confidence from 10 phrases spoken into the iPad. Used to set your VAD threshold.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)

                    Divider()

                    // Guided Calibration: PC-driven, pronunciation correction
                    Button("Guided Calibration (requires PC)") {
                        showVoiceCalibrationSheet = true
                    }
                    .disabled(wsManager.state != .connected)
                    .accessibilityHint("PC-driven session: corrects pronunciation errors and adapts recognition to today's condition.")

                    Text("PC plays prompts, scores your speech, and saves per-condition corrections (good day / flare / allergy / SVT). Requires bridge connection.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)

                    // G5: show and allow changing the current voice condition
                    LabeledContent("Active condition") {
                        Picker("", selection: $settings.voiceCondition) {
                            Text("Good day").tag("good_day")
                            Text("Flare day").tag("flare_day")
                            Text("Allergy day").tag("allergy_day")
                            Text("SVT attack").tag("svt_attack")
                        }
                        .pickerStyle(.menu)
                    }
                } header: {
                    DASectionHeader(title: "Voice Calibration")
                }
                .sheet(isPresented: $showVoiceProfilingSheet) {
                    VoiceProfilingSheet(settings: settings)
                }
                .sheet(isPresented: $showVoiceCalibrationSheet) {
                    VoiceCalibrationSheet(wsManager: wsManager)
                }

                // G7: Command Button backup
                Section {
                    Button("Export Command Buttons…") {
                        if let url = settings.exportCommandButtons() {
                            exportURL = url
                            showExportShareSheet = true
                        }
                    }
                    Text("Saves your command button layout as command_buttons.json in the Files app. Import on a new device to restore.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                } header: {
                    DASectionHeader(title: "Command Buttons Backup")
                }
                .sheet(isPresented: $showExportShareSheet) {
                    if let url = exportURL {
                        ShareSheet(items: [url])
                    }
                }

                // Re-run onboarding — for TestFlight updates that preserve
                // UserDefaults across reinstalls, so the wizard would otherwise
                // never appear again after the first completion.
                Section {
                    Button("Re-run Onboarding…") {
                        showRerunOnboardingAlert = true
                    }
                    .foregroundStyle(theme.accent)
                    .accessibilityHint("Restarts the calibration wizard from step 1.")

                    Text("Restarts the full calibration wizard (welcome → tilt → voice → gesture → flare → sound → touch → summary). Existing calibrations and settings are kept.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                } header: {
                    DASectionHeader(title: "Onboarding")
                }
                .alert("Re-run Onboarding?", isPresented: $showRerunOnboardingAlert) {
                    Button("Cancel", role: .cancel) {}
                    Button("Restart", role: .destructive) {
                        onboardingCurrentStep = 0
                        onboardingComplete = false
                    }
                } message: {
                    Text("The calibration wizard will reopen on the next screen. Your saved settings and calibrations are not erased.")
                }
            }
            .navigationTitle("Settings")
        }
    }

    private func _addKeyword() {
        let trimmed = newKeyword.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, !settings.keywordList.contains(trimmed) else { return }
        settings.keywordList.append(trimmed)
        newKeyword = ""
    }
}

// MARK: - ShareSheet helper (G7)

import UIKit

private struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
