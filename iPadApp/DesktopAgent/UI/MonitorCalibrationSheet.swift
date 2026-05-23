import ARKit
import Combine
import simd
import SwiftUI

/// 5-dot monitor calibration sheet.
///
/// The PC shows a bright cyan dot on the monitor at a known pixel position.
/// The user looks at that dot (eyes on the PC monitor, not the iPad),
/// then taps "Capture" here. The sheet reads the current ARKit world-space
/// gaze ray and sends it to the PC bridge along with the known pixel coords.
/// After 5 dots the PC solves the affine mapping and reports residual error.
struct MonitorCalibrationSheet: View {

    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var sensorManager: SensorManager
    @Environment(\.dismiss) private var dismiss
    @Environment(\.appTheme) private var theme

    // Current dot state
    @State private var dotIndex: Int = 0
    @State private var dotTotal: Int = 5
    @State private var dotLabel: String = "Waiting for PC…"
    @State private var dotPxX: Int = 0
    @State private var dotPxY: Int = 0

    // UI state
    @State private var phase: Phase = .waiting
    @State private var resultMessage: String = ""
    @State private var captureFeedback: String = ""

    private var cancellables = CancelBag()

    private enum Phase {
        case waiting       // PC hasn't sent first dot yet
        case capturing     // Actively capturing dots
        case complete      // Solved successfully
        case failed        // Solve or timeout failed
    }

    // Spatial labels map to compass positions for the hint diagram
    private let positionHints: [String: String] = [
        "TOP-LEFT":     "↖",
        "TOP-RIGHT":    "↗",
        "CENTER":       "⊕",
        "BOTTOM-LEFT":  "↙",
        "BOTTOM-RIGHT": "↘",
    ]

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {

                if phase == .complete {
                    completionView
                } else if phase == .failed {
                    failureView
                } else {
                    captureView
                }
            }
            .padding(DesignTokens.Spacing.lg)
            .navigationTitle("Monitor Calibration")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        dismiss()
                    }
                }
            }
        }
        .onAppear {
            wsManager.sendGazeCalibrationStart()
        }
        .onReceive(wsManager.gazeCalibrationFeed) { event in
            handleEvent(event)
        }
    }

    // MARK: - Sub-views

    private var captureView: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {

            // Progress
            HStack {
                Text(phase == .waiting ? "Waiting for PC to start…" : "Dot \(dotIndex + 1) of \(dotTotal)")
                    .font(DesignTokens.Typography.headline)
                    .foregroundStyle(theme.textPrimary)
                Spacer()
                if phase == .capturing {
                    ProgressView(value: Double(dotIndex), total: Double(dotTotal))
                        .frame(width: 100)
                        .tint(theme.accent)
                }
            }

            if phase == .capturing {
                // Spatial diagram — shows which corner of monitor to look at
                monitorDiagram

                Divider()

                // Instructions
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Text("Look at the \(dotLabel) cyan dot on your PC monitor.")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(theme.textPrimary)
                        .multilineTextAlignment(.center)

                    Text("Keep your gaze steady on the dot, then tap Capture.")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                        .multilineTextAlignment(.center)
                }

                if !captureFeedback.isEmpty {
                    Text(captureFeedback)
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.accent)
                }

                Spacer()

                DAButton(label: "Capture", icon: "camera.viewfinder") {
                    captureRay()
                }
                .disabled(!ARFaceTrackingConfiguration.isSupported)

                if !ARFaceTrackingConfiguration.isSupported {
                    Label("Gaze tracking requires TrueDepth camera (iPad Pro or iPhone X+).", systemImage: "eye.slash")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(.orange)
                }

            } else {
                // Waiting for first gaze_calibration_next from PC
                ProgressView()
                    .padding()
                Text("Make sure the PC is running and connected.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textSecondary)
                    .multilineTextAlignment(.center)
            }
        }
    }

    private var completionView: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.connected)

            Text("Calibration Complete")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text(resultMessage)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            Spacer()

            DAButton(label: "Done", icon: "checkmark") {
                dismiss()
            }
        }
    }

    private var failureView: some View {
        VStack(spacing: DesignTokens.Spacing.lg) {
            Image(systemName: "xmark.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(theme.disconnected)

            Text("Calibration Failed")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text(resultMessage)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            Spacer()

            HStack(spacing: DesignTokens.Spacing.md) {
                DAButton(label: "Try Again", icon: "arrow.counterclockwise") {
                    phase = .waiting
                    dotIndex = 0
                    dotLabel = "Waiting for PC…"
                    resultMessage = ""
                    wsManager.sendGazeCalibrationStart()
                }
                DAButton(label: "Close", icon: "xmark") {
                    dismiss()
                }
            }
        }
    }

    // MARK: - Monitor position diagram

    private var monitorDiagram: some View {
        let hint = positionHints[dotLabel] ?? "?"
        return ZStack {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(theme.textSecondary.opacity(0.3), lineWidth: 1.5)
                .frame(width: 200, height: 130)

            Text(hint)
                .font(.system(size: 36))
                .foregroundStyle(theme.accent)
                .position(diagramPosition(for: dotLabel, in: CGSize(width: 200, height: 130)))

            Text("Monitor")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
                .opacity(0.4)
        }
        .frame(width: 200, height: 130)
        .accessibilityLabel("Monitor diagram showing \(dotLabel) position")
    }

    private func diagramPosition(for label: String, in size: CGSize) -> CGPoint {
        let pad: CGFloat = 24
        switch label {
        case "TOP-LEFT":     return CGPoint(x: pad, y: pad)
        case "TOP-RIGHT":    return CGPoint(x: size.width - pad, y: pad)
        case "CENTER":       return CGPoint(x: size.width / 2, y: size.height / 2)
        case "BOTTOM-LEFT":  return CGPoint(x: pad, y: size.height - pad)
        case "BOTTOM-RIGHT": return CGPoint(x: size.width - pad, y: size.height - pad)
        default:             return CGPoint(x: size.width / 2, y: size.height / 2)
        }
    }

    // MARK: - Event handling

    private func handleEvent(_ event: GazeCalibrationEvent) {
        switch event {
        case .next(let idx, let pxX, let pxY, let total, let label):
            dotIndex = idx
            dotPxX   = pxX
            dotPxY   = pxY
            dotTotal = total
            dotLabel = label
            phase = .capturing
            captureFeedback = ""

        case .complete(let residual, let success, let message):
            if success {
                resultMessage = String(format: "Mapping solved — residual error: %.1f px.\nGaze cursor will now use absolute pixel positioning.", residual)
                phase = .complete
            } else {
                resultMessage = message ?? "Solve failed. Try again — ensure samples are spread across all 5 positions."
                phase = .failed
            }

        case .error(let msg):
            resultMessage = msg
            phase = .failed

        case .cancelled:
            dismiss()
        }
    }

    // MARK: - Ray capture

    private func captureRay() {
        let idx  = dotIndex
        let pxX  = dotPxX
        let pxY  = dotPxY

        sensorManager.gazeTracker.captureWorldRay { ray in
            guard let ray else {
                self.captureFeedback = "No gaze ray available — is gaze tracking active?"
                return
            }
            self.wsManager.sendGazeCalibrationSample(
                dotIndex: idx,
                pxX: pxX,
                pxY: pxY,
                rayDx: Double(ray.x),
                rayDy: Double(ray.y),
                rayDz: Double(ray.z)
            )
            self.captureFeedback = "Dot \(idx + 1) captured ✓"
        }
    }
}

// MARK: - Combine helpers

private final class CancelBag {
    var cancellables = Set<AnyCancellable>()
}
