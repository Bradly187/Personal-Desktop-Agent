import SwiftUI

/// Compact horizontal strip showing icons for all currently-running sensors.
/// Each icon pulses briefly when its sensor sends data.
/// Positioned below the navigation area, above content.
struct SensorActivityBar: View {
    @EnvironmentObject var sensorManager: SensorManager

    @Environment(\.appTheme) private var theme

    private var runningSensors: [SensorState] {
        sensorManager.sensorStates.filter { $0.isRunning }
    }

    var body: some View {
        if !runningSensors.isEmpty {
            HStack(spacing: DesignTokens.Spacing.md) {
                ForEach(runningSensors) { state in
                    SensorActivityDot(state: state)
                }
            }
            .padding(.horizontal, DesignTokens.Spacing.lg)
            .padding(.vertical, DesignTokens.Spacing.xs)
            .background(.regularMaterial, in: Capsule())
            .transition(.opacity.combined(with: .scale(scale: 0.9)))
            .animation(.easeInOut(duration: 0.2), value: runningSensors.map(\.id))
        }
    }
}

// MARK: - Individual sensor dot with icon

private struct SensorActivityDot: View {
    let state: SensorState
    @Environment(\.appTheme) private var theme

    var body: some View {
        Image(systemName: iconName)
            .font(.system(size: 14))
            .foregroundStyle(theme.accent)
            .frame(width: 24, height: 24)
            .accessibilityLabel("\(displayName) active")
    }

    private var iconName: String {
        switch state.id {
        case "tilt": return "ipad.landscape"
        case "gaze": return "eye"
        case "head": return "face.smiling"
        case "keyword": return "text.bubble"
        case "sound": return "mouth"
        case "audio": return "mic"
        case "lidar": return "sensor.tag.radiowaves.forward"
        default: return "circle.fill"
        }
    }

    private var displayName: String {
        switch state.id {
        case "tilt": return "Tilt"
        case "gaze": return "Gaze"
        case "head": return "Head"
        case "keyword": return "Keywords"
        case "sound": return "Sound"
        case "audio": return "Audio"
        case "lidar": return "LiDAR"
        default: return state.id
        }
    }
}
