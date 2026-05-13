import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var wsManager: WebSocketManager

    var body: some View {
        NavigationStack {
            Form {
                // Connection
                Section("Connection") {
                    LabeledContent("Host") {
                        TextField("192.168.1.100", text: $settings.serverHost)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.URL)
                            .autocorrectionDisabled()
                    }
                    LabeledContent("Port") {
                        TextField("8765", value: $settings.serverPort, format: .number)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numberPad)
                    }
                    Button("Reconnect") { wsManager.disconnect(); wsManager.connect() }
                }

                // Tilt
                Section("Tilt Navigation") {
                    Toggle("Enable Tilt", isOn: $settings.tiltEnabled)
                    LabeledContent("Sensitivity") {
                        Slider(value: $settings.tiltSensitivity, in: 0.1...5.0)
                    }
                    LabeledContent("Dead Zone") {
                        Slider(value: $settings.tiltDeadZone, in: 0.005...0.1)
                    }
                }

                // Gaze
                Section("Gaze & Dwell") {
                    Toggle("Enable Gaze", isOn: $settings.gazeEnabled)
                    LabeledContent("Dwell Timeout (s)") {
                        Slider(value: $settings.dwellTimeout, in: 0.3...3.0)
                            .overlay(alignment: .trailing) {
                                Text(String(format: "%.1f", settings.dwellTimeout))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .offset(y: 18)
                            }
                    }
                }

                // Head
                Section("Head Tracking") {
                    Toggle("Enable Head Tracking", isOn: $settings.headEnabled)
                    LabeledContent("Smoothing") {
                        Slider(value: $settings.headSmoothingFactor, in: 0.05...1.0)
                    }
                }

                // Trackpad
                Section("Trackpad") {
                    LabeledContent("Speed") {
                        Slider(value: $settings.trackpadSpeed, in: 0.5...5.0)
                    }
                    LabeledContent("Palm Reject Radius") {
                        Slider(value: $settings.palmRejectRadius, in: 10...60)
                    }
                }

                // Keywords
                Section("Voice Keywords") {
                    ForEach(settings.keywordList, id: \.self) { kw in
                        Text(kw)
                    }
                    .onDelete { idx in settings.keywordList.remove(atOffsets: idx) }
                    Button("Add keyword…") {
                        settings.keywordList.append("new keyword")
                    }
                }

                // Audio Streaming
                Section("Audio Streaming") {
                    Toggle("Stream mic to PC (Whisper)", isOn: $settings.audioStreamEnabled)
                    Text("Sends iPad microphone audio to the PC for full voice command processing via Whisper large-v3.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                // Sound mappings
                Section("Sound Mappings") {
                    ForEach(Array(settings.soundMappings.keys.sorted()), id: \.self) { sound in
                        HStack {
                            Text(sound)
                            Spacer()
                            Text(settings.soundMappings[sound] ?? "")
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("Settings")
        }
    }
}
