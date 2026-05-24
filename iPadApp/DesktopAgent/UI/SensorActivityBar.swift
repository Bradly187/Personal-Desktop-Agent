import SwiftUI

/// Compact horizontal strip showing icons for all currently-running sensors.
/// Each icon shows green when recently active, orange when running but silent
/// (possible hardware failure), and disappears when stopped.
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
                    SensorActivityDot(
                        state: state,
                        lastActivity: sensorManager.lastActivity[state.id]
                    )
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
    let lastActivity: Date?

    @Environment(\.appTheme) private var theme

    // G8: How long a running sensor can be silent before it's flagged as degraded.
    // High-frequency sensors (tilt/gaze) degrade after 2s; audio after 5s.
    private var maxSilenceSeconds: Double {
        switch state.id {
        case "tilt", "gaze", "head":  return 2.0
        case "audio", "keyword":      return 5.0
        default:                       return 3.0
        }
    }

    private var isDegraded: Bool {
        guard let last = lastActivity else { return true }  // running but never fired = degraded
        return Date().timeIntervalSince(last) > maxSilenceSeconds
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Image(systemName: iconName)
                .font(.system(size: 14))
                .foregroundStyle(isDegraded ? theme.warning : theme.accent)
                .frame(width: 24, height: 24)
            if isDegraded {
                Circle()
                    .fill(theme.warning)
                    .frame(width: 6, height: 6)
                    .offset(x: 2, y: -2)
            }
        }
        .accessibilityLabel(isDegraded ? "\(displayName) not responding" : "\(displayName) active")
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
