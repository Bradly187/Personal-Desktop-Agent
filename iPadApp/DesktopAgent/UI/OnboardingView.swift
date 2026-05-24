import ARKit
import AVFoundation
import CoreMotion
import SwiftUI

/// First-run onboarding wizard — 11 steps:
/// 0. Welcome        — what the app does
/// 1. Connection     — find the PC (includes start_agent.bat instruction)
/// 2. Hardware       — show available sensors on this device
/// 3. Permissions    — request camera / microphone before any sensor starts
/// 4. Cursor Control — pick primary cursor input method
/// 5. Voice & Sound  — enable voice commands and mouth sounds
/// 6. Calibration    — sensor-aware calibration hub
/// 7. Voice Profile  — acoustic baseline (requires PC)
/// 8. Gesture Check  — self-report gesture capability
/// 9. Flare Profile  — bad-day degradation setup
/// 10. Done          — calibration summary and launch
///
/// currentStep is persisted via @AppStorage so an interrupted session resumes.
/// onboardingComplete is set in UserDefaults only when the wizard is finished.
struct OnboardingView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var sensorManager: SensorManager
    @EnvironmentObject var serviceDiscovery: ServiceDiscovery

    @Environment(\.appTheme) private var theme

    @Binding var isComplete: Bool

    /// Persisted so interruptions (phone call, fatigue) resume where left off.
    @AppStorage("onboardingCurrentStep") private var currentStep = 0
    private let totalSteps = 11
    /// Tracks which calibrations were completed or explicitly skipped.
    @State private var calibrationsDone: Set<String> = []

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
                    // Fix P1: dedicated permissions step before any sensor starts
                    PermissionsStep()
                        .tag(3)
                    CursorControlStep(settings: settings)
                        .tag(4)
                    // Fix P2: Voice & Sound before Calibration so sound card appears
                    VoiceStep(settings: settings)
                        .tag(5)
                    CalibrationStep(
                        sensorManager: sensorManager,
                        settings: settings,
                        wsManager: wsManager,
                        done: $calibrationsDone
                    )
                    .tag(6)
                    // Steps 7–9: pass done binding so skip is tracked for Done summary
                    VoiceProfilingStep(settings: settings, done: $calibrationsDone)
                        .tag(7)
                    GestureAssessmentStep(settings: settings, done: $calibrationsDone)
                        .tag(8)
                    FlareProfileStep(settings: settings, done: $calibrationsDone)
                        .tag(9)
                    DoneStep(settings: settings, done: calibrationsDone)
                        .tag(10)
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
        currentStep = 0  // reset so a future re-run starts at welcome
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
    // G10: show "Continue offline" after 30 s with no connection
    @State private var searchSeconds: Int = 0
    @State private var searchTimer: Timer? = nil

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

            Text("The iPad talks to a bridge process on your PC. Start it first, then the app will find it automatically.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            // PC setup instruction
            HStack(alignment: .top, spacing: DesignTokens.Spacing.md) {
                Image(systemName: "desktopcomputer")
                    .font(.system(size: 22))
                    .foregroundStyle(theme.accent)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Start the PC bridge")
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(theme.textPrimary)
                    Text("Double-click start_agent.bat in the Personal Desktop Agent folder — or run: python main.py")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
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

            // G10: After 30 s without a connection, offer an offline path.
            // The user can still complete onboarding; PC-dependent calibrations
            // will be locked or skipped.
            if searchSeconds >= 30 && wsManager.state != .connected {
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Text("Can't find the PC bridge.")
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(theme.textPrimary)
                    Text("You can continue and complete sensor calibration later. PC-dependent steps (voice calibration, monitor mapping) will be locked until connected.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                        .multilineTextAlignment(.center)
                }
                .padding(DesignTokens.Spacing.lg)
                .background(theme.surfaceSecondary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .transition(.opacity.combined(with: .move(edge: .bottom)))
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .animation(.easeInOut(duration: 0.3), value: searchSeconds >= 30)
        .onAppear {
            serviceDiscovery.startBrowsing()
            // G10: Start 30-second timeout timer
            searchSeconds = 0
            searchTimer?.invalidate()
            searchTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
                if wsManager.state != .connected { searchSeconds += 1 }
                else { searchTimer?.invalidate() }
            }
        }
        .onDisappear {
            searchTimer?.invalidate()
            searchTimer = nil
        }
    }
}

// MARK: - Step 3: Hardware Detection

private struct HardwareStep: View {
    @Environment(\.appTheme) private var theme

    private let hasTrueDepth = ARFaceTrackingConfiguration.isSupported
    private let hasMotion = CMMotionManager().isDeviceMotionAvailable

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

// MARK: - Step 3: Permissions

private struct PermissionsStep: View {
    @Environment(\.appTheme) private var theme

    @State private var cameraStatus: PermStatus = .unknown
    @State private var micStatus: PermStatus = .unknown
    @State private var requested = false

    private enum PermStatus { case unknown, granted, denied }

    private var allHandled: Bool {
        cameraStatus != .unknown && micStatus != .unknown
    }

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: "lock.shield")
                .font(.system(size: 56))
                .foregroundStyle(theme.accent)
                .accessibilityHidden(true)

            Text("Allow Access")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("The app needs camera and microphone access. These are used only on your device — nothing is sent to the cloud.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            VStack(spacing: DesignTokens.Spacing.sm) {
                permRow(
                    icon: "camera.fill",
                    title: "Camera",
                    detail: "TrueDepth camera — eye gaze and head tracking",
                    status: cameraStatus
                )
                permRow(
                    icon: "mic.fill",
                    title: "Microphone",
                    detail: "Voice commands and mouth-sound detection",
                    status: micStatus
                )
                permRow(
                    icon: "gyroscope",
                    title: "Motion",
                    detail: "Accelerometer and gyroscope — tilt cursor control",
                    status: .granted  // Motion prompts automatically on first use
                )
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .padding(.horizontal, DesignTokens.Spacing.lg)

            if !requested {
                Button {
                    requestAll()
                } label: {
                    Text("Request Permissions")
                        .font(DesignTokens.Typography.body.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                        .background(theme.accent)
                        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }
                .padding(.horizontal, DesignTokens.Spacing.xl)
                .accessibilityHint("Double-tap to show iOS permission dialogs for camera and microphone")
            } else if allHandled {
                HStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(theme.success)
                    Text("Permissions handled — tap Next to continue.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            } else {
                HStack(spacing: DesignTokens.Spacing.sm) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for permission dialogs…")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .onAppear { checkExisting() }
    }

    private func permRow(icon: String, title: String, detail: String, status: PermStatus) -> some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: icon)
                .font(.system(size: 18))
                .foregroundStyle(theme.accent)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(theme.textPrimary)
                Text(detail)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
            }
            Spacer()
            Image(systemName: status == .granted ? "checkmark.circle.fill"
                           : status == .denied  ? "xmark.circle"
                           : "circle.dotted")
                .foregroundStyle(status == .granted ? theme.success
                               : status == .denied  ? theme.warning
                               : theme.textSecondary)
        }
    }

    private func checkExisting() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:           cameraStatus = .granted
        case .denied, .restricted:  cameraStatus = .denied
        default:                    cameraStatus = .unknown
        }
        switch AVAudioApplication.shared.recordPermission {
        case .granted:  micStatus = .granted
        case .denied:   micStatus = .denied
        default:        micStatus = .unknown
        }
        if cameraStatus != .unknown && micStatus != .unknown { requested = true }
    }

    private func requestAll() {
        requested = true
        AVCaptureDevice.requestAccess(for: .video) { granted in
            DispatchQueue.main.async { cameraStatus = granted ? .granted : .denied }
        }
        AVAudioApplication.requestRecordPermission { granted in
            DispatchQueue.main.async { micStatus = granted ? .granted : .denied }
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
                        subtitle: "Relative eye movement drives the cursor — look left to move left. Not eye-targeting; calibrate monitor mapping after setup for best accuracy.",
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

// MARK: - Step 5: Sensor Calibration Hub

private struct CalibrationStep: View {
    @ObservedObject var sensorManager: SensorManager
    @ObservedObject var settings: SettingsStore
    @ObservedObject var wsManager: WebSocketManager
    @Binding var done: Set<String>

    @Environment(\.appTheme) private var theme

    @State private var showGazeSensitivitySheet = false
    @State private var showMonitorCalSheet = false
    @State private var showSoundTrainingSheet = false
    @State private var gyroCountdown: Int? = nil

    private var isConnected: Bool { wsManager.state == .connected }
    private var hasTilt: Bool { settings.tiltEnabled }
    private var hasGaze: Bool { settings.gazeEnabled }
    private var hasHead: Bool { settings.headEnabled }
    private var hasSounds: Bool { !settings.soundMappings.isEmpty }
    private var hasAny: Bool { hasTilt || hasGaze || hasHead || hasSounds }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.xl) {

                // Header
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 48))
                        .foregroundStyle(theme.accent)
                    Text("Calibrate Your Sensors")
                        .font(DesignTokens.Typography.headline)
                        .foregroundStyle(theme.textPrimary)
                    Text("Complete the steps below for each sensor you've enabled. You can skip any and return later from Settings.")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textSecondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.top, DesignTokens.Spacing.lg)

                if !hasAny {
                    VStack(spacing: DesignTokens.Spacing.md) {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 48))
                            .foregroundStyle(theme.success)
                        Text("No calibration needed")
                            .font(DesignTokens.Typography.headline)
                            .foregroundStyle(theme.textPrimary)
                        Text("Trackpad mode needs no sensor calibration.")
                            .font(DesignTokens.Typography.body)
                            .foregroundStyle(theme.textSecondary)
                    }
                    .frame(maxWidth: .infinity)
                    .padding()
                } else {
                    VStack(alignment: .leading, spacing: DesignTokens.Spacing.xl) {

                        // TILT
                        if hasTilt {
                            calSection("Tilt Navigation", icon: "ipad.landscape") {
                                CalibrationCard(
                                    title: "Neutral Position",
                                    subtitle: "Hold iPad in your usual resting position, then tap.",
                                    isDone: done.contains("tilt_neutral"),
                                    isBlocked: false
                                ) {
                                    sensorManager.tiltSensor.calibrate()
                                    done.insert("tilt_neutral")
                                }
                                // Gyro bias only matters in velocity mode (not position mode)
                                if !settings.tiltPositionMode {
                                    CalibrationCard(
                                        title: "Gyro Bias",
                                        subtitle: gyroCountdown.map { "Hold still… \($0)s" }
                                            ?? "Place iPad on a flat surface, then tap to zero out sensor drift.",
                                        isDone: done.contains("tilt_gyro"),
                                        isBlocked: false
                                    ) {
                                        runGyroBias()
                                    }
                                }
                            }
                        }

                        // GAZE
                        if hasGaze {
                            calSection("Eye Gaze", icon: "eye") {
                                CalibrationCard(
                                    title: "Sensitivity Auto-Tune",
                                    subtitle: "Measures your eye-movement range (~15 s).",
                                    isDone: done.contains("gaze_sensitivity"),
                                    isBlocked: false
                                ) { showGazeSensitivitySheet = true }

                                CalibrationCard(
                                    title: "Monitor Mapping",
                                    subtitle: "Maps gaze to exact screen pixels via 5 dots (~2 min). Requires PC bridge.",
                                    isDone: done.contains("gaze_monitor"),
                                    isBlocked: !isConnected
                                ) { showMonitorCalSheet = true }
                            }
                        }

                        // HEAD
                        if hasHead {
                            calSection("Head Tracking", icon: "face.smiling") {
                                CalibrationCard(
                                    title: "Range Auto-Calibrates",
                                    subtitle: "Move your head left, right, up, down through your comfortable range. The system learns automatically — nothing to do here.",
                                    isDone: true,
                                    isBlocked: false
                                ) { }
                            }
                        }

                        // SOUND ACTIONS
                        if hasSounds {
                            calSection("Sound Actions", icon: "mouth") {
                                CalibrationCard(
                                    title: "Sound Training",
                                    subtitle: "Confirm cluck, pop, and hiss are detected by your device.",
                                    isDone: done.contains("sound_training"),
                                    isBlocked: false
                                ) { showSoundTrainingSheet = true }
                            }
                        }
                    }
                }

                // PC connection nudge for pending monitor calibration
                if hasGaze && !done.contains("gaze_monitor") && !isConnected {
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        Image(systemName: "wifi.exclamationmark")
                            .foregroundStyle(theme.warning)
                        Text("Start the PC bridge to unlock Monitor Mapping. You can finish it later from Settings → Gaze → Calibrate Monitor.")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textSecondary)
                    }
                    .padding(DesignTokens.Spacing.md)
                    .background(theme.surfaceSecondary)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                }

                Spacer(minLength: DesignTokens.Spacing.xxl)
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)
        }
        .onAppear {
            if hasTilt { sensorManager.tiltSensor.start() }
            if hasGaze { sensorManager.gazeTracker.start() }
            // Fix P4: start SoundDetector so SoundTrainingSheet receives detections
            if hasSounds { sensorManager.soundDetector.start() }
            if hasHead { done.insert("head_range") }
        }
        .sheet(isPresented: $showGazeSensitivitySheet, onDismiss: {
            done.insert("gaze_sensitivity")
        }) {
            GazeCalibrationSheet(sensorManager: sensorManager, settings: settings)
        }
        .sheet(isPresented: $showMonitorCalSheet, onDismiss: {
            done.insert("gaze_monitor")
        }) {
            MonitorCalibrationSheet()
                .environmentObject(wsManager)
                .environmentObject(sensorManager)
        }
        .sheet(isPresented: $showSoundTrainingSheet, onDismiss: {
            done.insert("sound_training")
        }) {
            SoundTrainingSheet(wsManager: wsManager, settings: settings)
        }
    }

    private func runGyroBias() {
        guard gyroCountdown == nil else { return }
        Task { @MainActor in
            for i in stride(from: 3, through: 1, by: -1) {
                gyroCountdown = i
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            gyroCountdown = nil
            // GyrobiasCalibirator inside FusionEngine captures bias automatically
            // when the iPad is stationary — prompting the user to hold still is enough.
            done.insert("tilt_gyro")
        }
    }

    @ViewBuilder
    private func calSection<C: View>(_ title: String, icon: String, @ViewBuilder content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            HStack(spacing: DesignTokens.Spacing.xs) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(theme.accent)
                Text(title.uppercased())
                    .font(.system(.caption2, design: .default).weight(.semibold))
                    .foregroundStyle(theme.textSecondary)
                    .kerning(0.8)
            }
            content()
        }
    }
}

// MARK: - CalibrationCard

private struct CalibrationCard: View {
    let title: String
    let subtitle: String
    let isDone: Bool
    let isBlocked: Bool
    let action: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Image(systemName: isDone ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 22))
                .foregroundStyle(isDone ? theme.success : theme.textSecondary)
                .frame(width: 26)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(isDone ? theme.textSecondary : theme.textPrimary)
                    .strikethrough(isDone)
                Text(subtitle)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if isBlocked {
                    Label("Needs PC bridge connection", systemImage: "wifi.exclamationmark")
                        .font(.system(size: 11))
                        .foregroundStyle(theme.warning)
                }
            }

            Spacer()

            if isDone {
                Text("Done")
                    .font(.system(.caption, design: .default).weight(.semibold))
                    .foregroundStyle(theme.success)
            } else if isBlocked {
                Image(systemName: "lock")
                    .font(.caption)
                    .foregroundStyle(theme.textSecondary)
            } else {
                Button(action: action) {
                    Text("Start")
                        .font(.system(.caption, design: .default).weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, DesignTokens.Spacing.md)
                        .padding(.vertical, DesignTokens.Spacing.sm)
                        .background(theme.accent)
                        .clipShape(Capsule())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(DesignTokens.Spacing.md)
        .background(theme.surfaceSecondary)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        .opacity(isBlocked ? 0.55 : 1.0)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title): \(isDone ? "complete" : isBlocked ? "locked, needs PC connection" : "pending")")
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
    @Binding var done: Set<String>
    @Environment(\.appTheme) private var theme
    @State private var showSheet = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: done.contains("voice_profiling")
                  ? "checkmark.circle.fill" : "waveform.badge.mic")
                .font(.system(size: 72))
                .foregroundStyle(done.contains("voice_profiling") ? theme.success : theme.accent)

            Text("Voice Profile")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Say 10 common commands aloud so the system can measure your natural voice volume and clarity — and stay accurate on harder days. Requires PC bridge connection.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            if !done.contains("voice_profiling") {
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

                Button("Skip for now") { done.insert("voice_profiling_skipped") }
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            } else {
                Text("Profile complete — you can re-run from Settings → Voice Calibration.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .sheet(isPresented: $showSheet, onDismiss: { done.insert("voice_profiling") }) {
            VoiceProfilingSheet(settings: settings)
        }
    }
}

// MARK: - Step 8: Gesture Assessment

private struct GestureAssessmentStep: View {
    @ObservedObject var settings: SettingsStore
    @Binding var done: Set<String>
    @Environment(\.appTheme) private var theme
    @State private var showSheet = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: done.contains("gesture_assessment")
                  ? "checkmark.circle.fill" : "hand.wave.fill")
                .font(.system(size: 72))
                .foregroundStyle(done.contains("gesture_assessment") ? theme.success : theme.accent)

            Text("Gesture Check")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Tell us which hand gestures you can do comfortably. The system adjusts confidence thresholds — especially on days when your hands are harder to control.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            if !done.contains("gesture_assessment") {
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

                Button("Skip for now") { done.insert("gesture_assessment_skipped") }
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            } else {
                Text("Assessment saved — re-run anytime from Settings → Gesture Assessment.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .sheet(isPresented: $showSheet, onDismiss: { done.insert("gesture_assessment") }) {
            GestureAssessmentSheet(settings: settings)
        }
    }
}

// MARK: - Step 9: Flare Profile

private struct FlareProfileStep: View {
    @ObservedObject var settings: SettingsStore
    @Binding var done: Set<String>
    @Environment(\.appTheme) private var theme
    @State private var showSheet = false

    var body: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Spacer()

            Image(systemName: done.contains("flare_profile")
                  ? "checkmark.circle.fill" : "heart.text.clipboard")
                .font(.system(size: 72))
                .foregroundStyle(done.contains("flare_profile") ? theme.success : theme.accent)

            Text("Flare Day Profile")
                .font(.system(.largeTitle, design: .rounded).weight(.bold))
                .foregroundStyle(theme.textPrimary)

            Text("Describe how your abilities change on hard days. The system pre-adapts before auto-detection would normally kick in — and you can flip a single toggle when a flare starts.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, DesignTokens.Spacing.lg)

            if !done.contains("flare_profile") {
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

                Button("Skip for now") { done.insert("flare_profile_skipped") }
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            } else {
                Text("Profile saved — update it anytime from Settings → Flare Profile.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
            }

            Spacer()
        }
        .padding(DesignTokens.Spacing.xl)
        .sheet(isPresented: $showSheet, onDismiss: { done.insert("flare_profile") }) {
            FlareProfileSheet(settings: settings)
        }
    }
}

// MARK: - Step 10: Done

private struct DoneStep: View {
    @ObservedObject var settings: SettingsStore
    let done: Set<String>

    @Environment(\.appTheme) private var theme

    // All calibration and setup items with relevance predicate.
    // Sensor calibrations (step 6) + profile steps (steps 7–9).
    private var calibrationItems: [(id: String, label: String, relevant: Bool)] { [
        ("tilt_neutral",        "Tilt neutral position",   settings.tiltEnabled),
        ("tilt_gyro",           "Gyro bias",               settings.tiltEnabled && !settings.tiltPositionMode),
        ("gaze_sensitivity",    "Gaze sensitivity",         settings.gazeEnabled),
        ("gaze_monitor",        "Monitor mapping",          settings.gazeEnabled),
        ("head_range",          "Head tracking",            settings.headEnabled),
        ("sound_training",      "Sound training",           !settings.soundMappings.isEmpty),
        ("voice_profiling",     "Voice profile",            settings.audioStreamEnabled),
        ("gesture_assessment",  "Gesture assessment",       true),
        ("flare_profile",       "Flare day profile",        true),
    ].filter { $0.relevant } }

    private func itemState(_ id: String) -> (isDone: Bool, isSkipped: Bool) {
        (done.contains(id), done.contains("\(id)_skipped"))
    }

    private var completedCount: Int {
        calibrationItems.filter { done.contains($0.id) }.count
    }
    private var totalCount: Int { calibrationItems.count }
    private var allDone: Bool { completedCount == totalCount }

    var body: some View {
        ScrollView {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer(minLength: DesignTokens.Spacing.xl)

                Image(systemName: allDone ? "checkmark.seal.fill" : "checkmark.seal")
                    .font(.system(size: 72))
                    .foregroundStyle(allDone ? theme.success : theme.accent)
                    .accessibilityHidden(true)

                Text(allDone ? "Fully Calibrated!" : "Almost Ready!")
                    .font(.system(.largeTitle, design: .rounded).weight(.bold))
                    .foregroundStyle(theme.textPrimary)

                Text(allDone
                    ? "All sensors are calibrated. Your Desktop Agent is ready."
                    : "Some calibrations were skipped. You can complete them anytime from Settings.")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, DesignTokens.Spacing.xl)

                // Calibration summary
                if !calibrationItems.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        HStack {
                            Text("Calibration Summary")
                                .font(.system(.caption2, design: .default).weight(.semibold))
                                .foregroundStyle(theme.textSecondary)
                                .kerning(0.8)
                            Spacer()
                            Text("\(completedCount)/\(totalCount)")
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(completedCount == totalCount ? theme.success : theme.warning)
                        }
                        .padding(.horizontal, DesignTokens.Spacing.lg)
                        .padding(.bottom, DesignTokens.Spacing.sm)

                        VStack(spacing: 0) {
                            ForEach(calibrationItems, id: \.id) { item in
                                let state = itemState(item.id)
                                HStack(spacing: DesignTokens.Spacing.md) {
                                    Image(systemName: state.isDone
                                          ? "checkmark.circle.fill"
                                          : state.isSkipped ? "arrow.right.circle"
                                          : "exclamationmark.circle")
                                        .foregroundStyle(state.isDone ? theme.success
                                                       : state.isSkipped ? theme.textSecondary
                                                       : theme.warning)
                                        .font(.system(size: 16))
                                    Text(item.label)
                                        .font(DesignTokens.Typography.body)
                                        .foregroundStyle(theme.textPrimary)
                                    Spacer()
                                    Text(state.isDone ? "Done"
                                       : state.isSkipped ? "Skipped"
                                       : "Not done")
                                        .font(DesignTokens.Typography.caption)
                                        .foregroundStyle(state.isDone ? theme.success : theme.textSecondary)
                                }
                                .padding(.horizontal, DesignTokens.Spacing.lg)
                                .padding(.vertical, DesignTokens.Spacing.md)
                                if item.id != calibrationItems.last?.id {
                                    Divider().padding(.leading, 52)
                                }
                            }
                        }
                        .background(theme.surfaceSecondary)
                        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                        .padding(.horizontal)
                    }
                }

                // Tips
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                    tipRow(icon: "hand.tap",        text: "Swipe between tabs or drag the tab bar")
                    tipRow(icon: "gearshape",       text: "All calibrations are re-runnable from Settings")
                    tipRow(icon: "sensor.tag.radiowaves.forward", text: "Tap Sensors tab to see live status")
                }
                .padding(DesignTokens.Spacing.lg)
                .background(theme.surfaceSecondary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
                .padding(.horizontal, DesignTokens.Spacing.xl)

                Spacer(minLength: DesignTokens.Spacing.xxl)
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)
        }
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
