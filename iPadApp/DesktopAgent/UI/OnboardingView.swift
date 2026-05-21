import SwiftUI
import ARKit
import CoreMotion

/// First-run onboarding wizard that guides new users through:
/// 1. Welcome — what the app does
/// 2. Connection — find the PC (mDNS auto-discovery or manual)
/// 3. Hardware — show available sensors on this device
/// 4. Cursor Control — pick primary cursor input method
/// 5. Calibration — tilt neutral calibration (if tilt selected)
/// 6. Voice — enable keywords / audio streaming
/// 7. Done — summary and launch
///
/// Persists `onboardingComplete` to UserDefaults so it only shows once.
struct OnboardingView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var sensorManager: SensorManager
    @EnvironmentObject var serviceDiscovery: ServiceDiscovery

    @Environment(\.appTheme) private var theme

    @Binding var isComplete: Bool

    @State private var currentStep = 0
    private let totalSteps = 10

    var body: some View {
        ZStack {
            theme.surfacePrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                // Progress indicator
                progressBar
                    .padding(.horizontal, DesignTokens.Spacing.xl)
                    .padding(.top, DesignTokens.Spacing.lg)

                // Step content
                TabView(selection: $currentStep) {
                    WelcomeStep()
                        .tag(0)
                    ConnectionStep(wsManager: wsManager, settings: settings, serviceDiscovery: serviceDiscovery)
                        .tag(1)
                    HardwareStep()
                        .tag(2)
                    CursorControlStep(settings: settings)
                        .tag(3)
                    CalibrationStep(sensorManager: sensorManager, settings: settings)
                        .tag(4)
                    VoiceStep(settings: settings)
                        .tag(5)
                    VoiceProfilingStep(settings: settings)
                        .tag(6)
                    GestureAssessmentStep(settings: settings)
                        .tag(7)
                    FlareProfileStep(settings: settings)
                        .tag(8)
                    DoneStep()
                        .tag(9)
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .animation(.easeInOut(duration: 0.3), value: currentStep)

                // Navigation buttons
                navigationBar
                    .padding(.horizontal, DesignTokens.Spacing.xl)
                    .padding(.bottom, DesignTokens.Spacing.xl)
            }
        }
    }

    // MARK: - Progress Bar

    private var progressBar: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 3)
                    .fill(theme.surfaceSecondary)
                    .frame(height: 6)
                RoundedRectangle(cornerRadius: 3)
                    .fill(theme.accent)
                    .frame(width: geo.size.width * CGFloat(currentStep + 1) / CGFloat(totalSteps), height: 6)
                    .animation(.easeInOut(duration: 0.3), value: currentStep)
            }
        }
        .frame(height: 6)
        .accessibilityLabel("Step \(currentStep + 1) of \(totalSteps)")
    }

    // MARK: - Navigation

    private var navigationBar: some View {
        HStack {
            if currentStep > 0 {
                Button("Back") {
                    currentStep -= 1
                }
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                .accessibilityLabel("Go back")
            }

            Spacer()

            if currentStep < totalSteps - 1 {
                Button {
                    currentStep += 1
                } label: {
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        Text(currentStep == 0 ? "Get Started" : "Next")
                        Image(systemName: "arrow.right")
                    }
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, DesignTokens.Spacing.xl)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }
                .accessibilityLabel(currentStep == 0 ? "Get started" : "Next step")
            } else {
                Button {
                    completeOnboarding()
                } label: {
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        Text("Launch App")
                        Image(systemName: "checkmark")
                    }
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, DesignTokens.Spacing.xl)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.success)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }
                .accessibilityLabel("Complete setup and launch app")
            }
        }
    }

    private func completeOnboarding() {
        UserDefaults.standard.set(true, forKey: "onboardingComplete")
        // Start sensors and connect based on selections made during onboarding
        wsManager.settings = settings
        wsManager.serviceDiscovery = serviceDiscovery
        serviceDiscovery.startBrowsing()
        sensorManager.startAll()
        wsManager.connect()
        isComplete = true
    }
}

// MARK: - Step 1: Welcome

private struct WelcomeStep: View {
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "hand.point.up.braille")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            Text("Desktop Agent")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Control your computer hands-free using your iPad's sensors — tilt, gaze, head movement, voice, and touch.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.xl)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                featureRow(icon: "ipad.landscape", text: "Tilt your iPad to move the cursor")
                featureRow(icon: "eye", text: "Use eye gaze for fine cursor control")
                featureRow(icon: "mic", text: "Speak commands or use mouth sounds")
                featureRow(icon: "hand.draw", text: "Touch trackpad and handwriting input")
            }
            .padding(.horizontal, DesignTokens.Spacing.xxl)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
    }

    private func featureRow(icon: String, text: String) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: icon)
                .font(.system(size: 20))
                .foregroundStyle(theme.accent)
                .frame(width: 32)
            Text(text)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textPrimary)
        }
    }
}

// MARK: - Step 2: Connection

private struct ConnectionStep: View {
    @ObservedObject var wsManager: WebSocketManager
    @ObservedObject var settings: SettingsStore
    @ObservedObject var serviceDiscovery: ServiceDiscovery

    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "wifi")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            Text("Connect to Your PC")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("Make sure the Desktop Agent bridge is running on your PC. The app will try to find it automatically.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            // Auto-discovery status
            VStack(spacing: DesignTokens.Spacing.md) {
                if let host = serviceDiscovery.discoveredHost,
                   let port = serviceDiscovery.discoveredPort {
                    HStack(spacing: DesignTokens.Spacing.md) {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(theme.success)
                            .font(.system(size: 24))
                        VStack(alignment: .leading) {
                            Text("Found!")
                                .font(DesignTokens.Typography.body.weight(.semibold))
                                .foregroundStyle(theme.textPrimary)
                            Text("\(host):\(port)")
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(theme.textSecondary)
                        }
                    }
                    .padding(DesignTokens.Spacing.lg)
                    .background(theme.surfaceSecondary)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                } else {
                    HStack(spacing: DesignTokens.Spacing.md) {
                        ProgressView()
                        Text("Searching on local network…")
                            .font(DesignTokens.Typography.body)
                            .foregroundStyle(theme.textSecondary)
                    }
                    .padding(DesignTokens.Spacing.lg)
                }
            }

            // Manual entry
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
                Text("Or enter manually:")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                HStack(spacing: DesignTokens.Spacing.md) {
                    TextField("IP Address", text: $settings.serverHost)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .textFieldStyle(.roundedBorder)
                    TextField("Port", text: Binding(
                        get: { String(settings.serverPort) },
                        set: { settings.serverPort = Int($0.filter(\.isWholeNumber)) ?? 8765 }
                    ))
                    .keyboardType(.numberPad)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
                }
            }
            .padding(.horizontal, DesignTokens.Spacing.xl)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .onAppear {
            serviceDiscovery.startBrowsing()
        }
    }
}

// MARK: - Step 3: Hardware Detection

private struct HardwareStep: View {
    @Environment(\.appTheme) private var theme

    private let hasTrueDepth = ARFaceTrackingConfiguration.isSupported
    private let hasMotion = CMMotionManager().isDeviceMotionAvailable
    private let hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "sensor.tag.radiowaves.forward")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            Text("Your iPad's Sensors")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("Here's what your device supports:")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                sensorRow(name: "Accelerometer & Gyroscope", detail: "Tilt navigation", available: hasMotion)
                sensorRow(name: "TrueDepth Camera", detail: "Eye gaze & head tracking", available: hasTrueDepth)
                sensorRow(name: "Microphone", detail: "Voice commands & sound actions", available: true)
                sensorRow(name: "LiDAR Scanner", detail: "Hand gesture recognition", available: hasLiDAR)
                sensorRow(name: "Multi-Touch Display", detail: "Trackpad & handwriting", available: true)
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .padding(.horizontal, DesignTokens.Spacing.lg)

            if !hasTrueDepth {
                Text("Eye gaze and head tracking require a TrueDepth camera (iPad Pro 2020+).")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.warning)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, DesignTokens.Spacing.xl)
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
    }

    private func sensorRow(name: String, detail: String, available: Bool) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: available ? "checkmark.circle.fill" : "xmark.circle")
                .foregroundStyle(available ? theme.success : theme.textSecondary)
                .font(.system(size: 20))
            VStack(alignment: .leading, spacing: 2) {
                Text(name)
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textPrimary)
                Text(detail)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
            }
            Spacer()
        }
    }
}

// MARK: - Step 4: Cursor Control

private struct CursorControlStep: View {
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme

    private let hasTrueDepth = ARFaceTrackingConfiguration.isSupported

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "cursorarrow.motionlines")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            Text("How do you want to move the cursor?")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)
                .multilineTextAlignment(.center)

            Text("Pick your primary method. You can always change this later in Settings.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            VStack(spacing: DesignTokens.Spacing.md) {
                cursorOption(
                    icon: "ipad.landscape",
                    title: "Tilt",
                    subtitle: "Tilt your iPad to position the cursor",
                    isSelected: settings.tiltEnabled && !settings.gazeEnabled && !settings.headEnabled,
                    action: { selectTilt() }
                )

                if hasTrueDepth {
                    cursorOption(
                        icon: "eye",
                        title: "Eye Gaze",
                        subtitle: "Move cursor by looking in different directions",
                        isSelected: settings.gazeEnabled && !settings.tiltEnabled,
                        action: { selectGaze() }
                    )

                    cursorOption(
                        icon: "face.smiling",
                        title: "Head Movement",
                        subtitle: "Turn your head to move the cursor",
                        isSelected: settings.headEnabled && !settings.tiltEnabled && !settings.gazeEnabled,
                        action: { selectHead() }
                    )
                }

                cursorOption(
                    icon: "hand.point.up",
                    title: "Trackpad Only",
                    subtitle: "Use the on-screen trackpad with touch",
                    isSelected: !settings.tiltEnabled && !settings.gazeEnabled && !settings.headEnabled,
                    action: { selectTrackpadOnly() }
                )
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
    }

    private func cursorOption(icon: String, title: String, subtitle: String, isSelected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: DesignTokens.Spacing.md) {
                Image(systemName: icon)
                    .font(.system(size: DesignTokens.Size.iconSizeLarge))
                    .foregroundStyle(isSelected ? theme.accent : theme.textSecondary)
                    .frame(width: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(theme.textPrimary)
                    Text(subtitle)
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
                Spacer()
                if isSelected {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(theme.accent)
                        .font(.system(size: 22))
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

    private func selectTilt() {
        settings.tiltEnabled = true
        settings.gazeEnabled = false
        settings.headEnabled = false
    }

    private func selectGaze() {
        settings.gazeEnabled = true
        settings.tiltEnabled = false
        settings.headEnabled = false
    }

    private func selectHead() {
        settings.headEnabled = true
        settings.tiltEnabled = false
        settings.gazeEnabled = false
    }

    private func selectTrackpadOnly() {
        settings.tiltEnabled = false
        settings.gazeEnabled = false
        settings.headEnabled = false
    }
}

// MARK: - Step 5: Calibration

private struct CalibrationStep: View {
    @ObservedObject var sensorManager: SensorManager
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme

    @State private var calibrated = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: settings.tiltEnabled ? "level" : "arrow.right.circle")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            if settings.tiltEnabled {
                Text("Calibrate Tilt")
                    .font(DesignTokens.Typography.headline)
                    .foregroundStyle(theme.textPrimary)

                Text("Hold your iPad in the position you'll normally use it, then tap the button below. This sets the \"center\" position for cursor control.")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, DesignTokens.Spacing.lg)

                Button {
                    sensorManager.tiltSensor.calibrate()
                    calibrated = true
                } label: {
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        Image(systemName: calibrated ? "checkmark" : "scope")
                        Text(calibrated ? "Calibrated!" : "Set Neutral Position")
                    }
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, DesignTokens.Spacing.xl)
                    .frame(minHeight: DesignTokens.Size.touchTargetMin)
                    .background(calibrated ? theme.success : theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }
                .accessibilityLabel(calibrated ? "Calibration complete" : "Calibrate neutral tilt position")

                if calibrated {
                    Text("You can recalibrate anytime from Settings.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            } else {
                Text("Calibration")
                    .font(DesignTokens.Typography.headline)
                    .foregroundStyle(theme.textPrimary)

                Text("No calibration needed for your selected cursor method. You're all set!")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, DesignTokens.Spacing.lg)

                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 48))
                    .foregroundStyle(theme.success)
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .onAppear {
            // Start tilt sensor temporarily for calibration
            if settings.tiltEnabled {
                sensorManager.tiltSensor.start()
            }
        }
    }
}

// MARK: - Step 6: Voice

private struct VoiceStep: View {
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "waveform")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            Text("Voice & Sound Input")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("Enable voice commands and mouth sounds for hands-free actions.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            VStack(spacing: DesignTokens.Spacing.md) {
                voiceOption(
                    icon: "text.bubble",
                    title: "Voice Keywords",
                    subtitle: "Say \"click\", \"scroll\", or \"open\" to trigger actions",
                    isEnabled: !settings.keywordList.isEmpty,
                    toggle: {
                        if settings.keywordList.isEmpty {
                            settings.keywordList = ["click", "scroll", "open"]
                        } else {
                            settings.keywordList = []
                        }
                    }
                )

                voiceOption(
                    icon: "mouth",
                    title: "Sound Actions",
                    subtitle: "Cluck to click, pop to scroll down, hiss to scroll up",
                    isEnabled: !settings.soundMappings.isEmpty,
                    toggle: {
                        if settings.soundMappings.isEmpty {
                            settings.soundMappings = ["cluck": "CLICK", "pop": "SCROLL down", "hiss": "SCROLL up"]
                        } else {
                            settings.soundMappings = [:]
                        }
                    }
                )

                voiceOption(
                    icon: "mic.badge.plus",
                    title: "Full Voice (Whisper)",
                    subtitle: "Stream audio to PC for natural language commands",
                    isEnabled: settings.audioStreamEnabled,
                    toggle: { settings.audioStreamEnabled.toggle() }
                )
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
    }

    private func voiceOption(icon: String, title: String, subtitle: String, isEnabled: Bool, toggle: @escaping () -> Void) -> some View {
        Button(action: toggle) {
            HStack(spacing: DesignTokens.Spacing.md) {
                Image(systemName: icon)
                    .font(.system(size: DesignTokens.Size.iconSizeLarge))
                    .foregroundStyle(isEnabled ? theme.accent : theme.textSecondary)
                    .frame(width: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(theme.textPrimary)
                    Text(subtitle)
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
                Spacer()
                Image(systemName: isEnabled ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(isEnabled ? theme.accent : theme.textSecondary)
                    .font(.system(size: 22))
            }
            .padding(DesignTokens.Spacing.lg)
            .background(isEnabled ? theme.accent.opacity(0.1) : theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(title): \(isEnabled ? "enabled" : "disabled")")
        .accessibilityHint("Double-tap to toggle")
    }
}

// MARK: - Step 7: Voice Profiling

private struct VoiceProfilingStep: View {
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @State private var showSheet = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "waveform.badge.mic")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)

            Text("Voice Profile")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Say 10 common commands aloud so the system can measure your natural voice volume and clarity — and stay accurate on harder days.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            Button {
                showSheet = true
            } label: {
                Label("Start Voice Profiling", systemImage: "mic.fill")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    .padding(.horizontal, DesignTokens.Spacing.xl)
            }

            Button("Skip for now") { }
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .sheet(isPresented: $showSheet) {
            VoiceProfilingSheet(settings: settings)
        }
    }
}

// MARK: - Step 8: Gesture Assessment

private struct GestureAssessmentStep: View {
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @State private var showSheet = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "hand.wave.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)

            Text("Gesture Check")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Tell us which hand gestures you can do comfortably. The system adjusts confidence thresholds — especially on days when your hands are harder to control.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            Button {
                showSheet = true
            } label: {
                Label("Start Assessment", systemImage: "hand.raised.fill")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    .padding(.horizontal, DesignTokens.Spacing.xl)
            }

            Button("Skip for now") { }
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .sheet(isPresented: $showSheet) {
            GestureAssessmentSheet(settings: settings)
        }
    }
}

// MARK: - Step 9: Flare Profile

private struct FlareProfileStep: View {
    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @State private var showSheet = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "heart.text.clipboard")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)

            Text("Flare Day Profile")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Describe how your abilities change on hard days. The system pre-adapts before auto-detection would normally kick in — and you can flip a single toggle when a flare starts.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            Button {
                showSheet = true
            } label: {
                Label("Set Up Flare Profile", systemImage: "heart.fill")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                    .padding(.horizontal, DesignTokens.Spacing.xl)
            }

            Button("Skip for now") { }
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .sheet(isPresented: $showSheet) {
            FlareProfileSheet(settings: settings)
        }
    }
}

// MARK: - Step 10: Done

private struct DoneStep: View {
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.success)
                .accessibilityHidden(true)

            Text("You're All Set!")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Your Desktop Agent is configured and ready to go. You can adjust any setting later from the Settings tab.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.xl)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                tipRow(icon: "hand.tap", text: "Swipe between tabs or drag the tab bar")
                tipRow(icon: "gearshape", text: "Fine-tune sensors in Settings")
                tipRow(icon: "questionmark.circle", text: "Tap Sensors tab to see live status")
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .padding(.horizontal, DesignTokens.Spacing.xl)

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
    }

    private func tipRow(icon: String, text: String) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: icon)
                .font(.system(size: 18))
                .foregroundStyle(theme.accent)
                .frame(width: 28)
            Text(text)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textPrimary)
        }
    }
}
