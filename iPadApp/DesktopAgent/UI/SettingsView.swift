import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var serviceDiscovery: ServiceDiscovery

    @Environment(\.appTheme) private var theme

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
                } header: {
                    DASectionHeader(title: "Connection")
                }

                // Tilt
                Section {
                    Toggle("Enable Tilt", isOn: $settings.tiltEnabled)
                    LabeledContent("Sensitivity") {
                        Slider(value: $settings.tiltSensitivity, in: 0.1...5.0)
                    }
                    LabeledContent("Dead Zone") {
                        Slider(value: $settings.tiltDeadZone, in: 0.005...0.1)
                    }
                } header: {
                    DASectionHeader(title: "Tilt Navigation")
                }

                // Gaze
                Section {
                    Toggle("Enable Gaze", isOn: $settings.gazeEnabled)
                    LabeledContent("Dwell Timeout (s)") {
                        Slider(value: $settings.dwellTimeout, in: 0.3...3.0)
                            .overlay(alignment: .trailing) {
                                Text(String(format: "%.1f", settings.dwellTimeout))
                                    .font(DesignTokens.Typography.caption)
                                    .foregroundStyle(theme.textSecondary)
                                    .offset(y: 18)
                            }
                    }
                } header: {
                    DASectionHeader(title: "Gaze & Dwell")
                }

                // Head
                Section {
                    Toggle("Enable Head Tracking", isOn: $settings.headEnabled)
                    LabeledContent("Smoothing") {
                        Slider(value: $settings.headSmoothingFactor, in: 0.05...1.0)
                    }
                } header: {
                    DASectionHeader(title: "Head Tracking")
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
                    ForEach(settings.keywordList, id: \.self) { kw in
                        Text(kw)
                    }
                    .onDelete { idx in settings.keywordList.remove(atOffsets: idx) }
                    Button("Add keyword…") {
                        settings.keywordList.append("new keyword")
                    }
                    .accessibilityHint("Double-tap to add a new voice keyword")
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
                    ForEach(Array(settings.soundMappings.keys.sorted()), id: \.self) { sound in
                        HStack {
                            Text(sound)
                                .foregroundStyle(theme.textPrimary)
                            Spacer()
                            Text(settings.soundMappings[sound] ?? "")
                                .foregroundStyle(theme.textSecondary)
                        }
                    }
                } header: {
                    DASectionHeader(title: "Sound Mappings")
                }
            }
            .navigationTitle("Settings")
        }
    }
}
