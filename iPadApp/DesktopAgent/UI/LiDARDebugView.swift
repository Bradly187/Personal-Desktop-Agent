import SwiftUI

/// Debug view showing the live LiDAR depth map and rear camera feed.
///
/// Camera image (top) and colour-mapped depth image (bottom) are rendered
/// directly from LiDARStreamer's @Published UIImages — no round-trip to the PC.
///
/// Stats bar shows per-stream fps, valid depth pixel %, and depth range.
/// The toolbar Start/Stop button toggles settings.lidarEnabled so the
/// preference persists across launches.
struct LiDARDebugView: View {
    @EnvironmentObject var settings: SettingsStore
    @EnvironmentObject var sensorManager: SensorManager

    @Environment(\.appTheme) private var theme

    private var streamer: LiDARStreamer { sensorManager.lidarStreamer }

    var body: some View {
        NavigationStack {
            GeometryReader { geo in
                VStack(spacing: 0) {
                    feedPanel(
                        title: "Camera",
                        image: streamer.latestCameraImage,
                        fps: streamer.cameraFps,
                        placeholder: "camera",
                        height: geo.size.height * 0.44
                    )

                    statsBar
                        .frame(height: geo.size.height * 0.10)

                    feedPanel(
                        title: "Depth (blue=near, red=far, 0–4 m)",
                        image: streamer.latestDepthImage,
                        fps: streamer.depthFps,
                        placeholder: "sensor.tag.radiowaves.forward",
                        height: geo.size.height * 0.44
                    )
                }
            }
            .navigationTitle("Sensors")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button(streamer.isRunning ? "Stop" : "Start") {
                        settings.lidarEnabled.toggle()
                    }
                    .disabled(!LiDARStreamer.isSupported)
                }
            }
            .overlay {
                if !LiDARStreamer.isSupported {
                    unavailableOverlay
                }
            }
        }
    }

    // MARK: - Subviews

    private func feedPanel(
        title: String,
        image: UIImage?,
        fps: Double,
        placeholder: String,
        height: CGFloat
    ) -> some View {
        ZStack {
            Color.black

            if let img = image {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFit()
                    .accessibilityLabel(title)
            } else {
                VStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: placeholder)
                        .font(.system(size: 40))
                        .foregroundStyle(theme.textSecondary)
                    Text(streamer.isRunning ? "Waiting for first frame…" : "Tap Start to begin")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(theme.textSecondary)
                }
            }

            // Title + fps badge in top-left corner
            VStack {
                HStack {
                    Text("\(title)  \(fps > 0 ? String(format: "%.0f fps", fps) : "")")
                        .font(DesignTokens.Typography.caption)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Color.black.opacity(0.55))
                        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                    Spacer()
                }
                .padding(DesignTokens.Spacing.sm)
                Spacer()
            }
        }
        .frame(height: height)
        .clipped()
    }

    private var statsBar: some View {
        HStack(spacing: DesignTokens.Spacing.lg) {
            statCell(
                label: "Valid px",
                value: String(format: "%.0f%%", streamer.validPixelPct)
            )
            Divider().frame(height: 28)
            statCell(
                label: "Min depth",
                value: String(format: "%.2f m", streamer.depthRangeMin)
            )
            Divider().frame(height: 28)
            statCell(
                label: "Max depth",
                value: String(format: "%.2f m", streamer.depthRangeMax)
            )
            Divider().frame(height: 28)
            statCell(
                label: "Status",
                value: streamer.isRunning ? "Running" : "Stopped"
            )
        }
        .padding(.horizontal, DesignTokens.Spacing.lg)
        .frame(maxWidth: .infinity)
        .background(theme.surfaceSecondary)
    }

    private func statCell(label: String, value: String) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textPrimary)
                .monospacedDigit()
            Text(label)
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
        }
    }

    private var unavailableOverlay: some View {
        ZStack {
            Color.black.opacity(0.7)
            VStack(spacing: DesignTokens.Spacing.md) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 48))
                    .foregroundStyle(.orange)
                Text("LiDAR not available")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(.white)
                Text("Requires iPad Pro 2020+ or iPhone 12 Pro or later.")
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(.white.opacity(0.7))
                    .multilineTextAlignment(.center)
            }
            .padding(DesignTokens.Spacing.xl)
        }
        .ignoresSafeArea()
    }
}
